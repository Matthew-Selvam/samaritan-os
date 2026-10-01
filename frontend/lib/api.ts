/**
 * api.ts — typed HTTP/WS client for the Signal-OS backend.
 *
 * Contract: `docs/CONTRACTS.md` §10 (routes) + §11 (frontend client rules).
 * Design rules enforced here:
 *  - the base URL resolves to same-origin `""` by default so the Next.js server
 *    proxies `/api` + `/ws` to the backend — no host is ever hardcoded;
 *  - every request is typed, abortable via `AbortController`, and surfaces the
 *    backend's `detail` / `error` text instead of swallowing it;
 *  - transient gateway errors (502/503/504) retry exactly once with backoff.
  */
 import type {
   AgentMeta,
   AgentResult,
   AgentStatus,
   Case,
   CaseStats,
   Entity,
   HealthStatus,
   Investigation,
   MetricsSnapshot,
   NameSearchRequest,
   OpsecStatus,
   Paged,
   PhotoSearchResult,
   Report,
   ReportExport,
   ReportFormat,
   SearchOptions,
     SearchRequest,
     SearchResponse,
     SubmitInvestigationOptions,
     TimelineEvent,
   } from "./types";

// ── Base URL resolution ──────────────────────────────────────────────────────

function stripTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

/**
 * Raw `NEXT_PUBLIC_API_URL`, or `""` when unset.
 * @returns the configured origin with no trailing slash (possibly empty).
 */
export function rawApiBase(): string {
  const raw = process.env.NEXT_PUBLIC_API_URL;
  if (!raw || !raw.trim()) return "";
  return stripTrailingSlash(raw.trim());
}

/**
 * Resolved API base. `""` means "same origin" — the browser issues relative
 * requests and the frontend's rewrite/proxy forwards them to the backend.
 */
export const API_BASE: string = rawApiBase();

/**
 * Optional API key. Sent as `X-API-Key` when `NEXT_PUBLIC_SIGNAL_API_KEY` is set;
 * absent, the backend falls back to its anonymous/dev principal.
 */
export const API_KEY: string = (process.env.NEXT_PUBLIC_SIGNAL_API_KEY ?? "").trim();

const IS_PRODUCTION = process.env.NODE_ENV === "production";

let warnedInsecureBase = false;

/**
 * Warn once when production points at a bare `http://` host: that is either a
 * mixed-content failure (page https, API http) or cleartext traffic.
 */
function warnInsecureBaseOnce(): void {
  if (warnedInsecureBase) return;
  if (!IS_PRODUCTION || !API_BASE) return;
  if (!/^http:\/\//i.test(API_BASE)) return;
  warnedInsecureBase = true;
  console.warn(
    "[signal-os] NEXT_PUBLIC_API_URL uses http:// in a production build. " +
      "Use https:// (or leave it unset to proxy same-origin), otherwise the " +
      "browser blocks the request as mixed content.",
  );
}

// ── Errors ──────────────────────────────────────────────────────────────────

/** Structured HTTP failure carrying the backend's own message. */
export class ApiError extends Error {
  readonly status: number;
  readonly statusText: string;
  readonly url: string;
  readonly method: string;
  /** Parsed response body when it was JSON, otherwise the raw text. */
  readonly body: unknown;

  constructor(params: {
    message: string;
    status: number;
    statusText?: string;
    url: string;
    method: string;
    body?: unknown;
  }) {
    super(params.message);
    this.name = "ApiError";
    this.status = params.status;
    this.statusText = params.statusText ?? "";
    this.url = params.url;
    this.method = params.method;
    this.body = params.body;
    // Keep `instanceof ApiError` working when the class is down-levelled to ES5.
    Object.setPrototypeOf(this, ApiError.prototype);
  }

  /** True for network/abort failures that never reached the backend. */
  get isTransport(): boolean {
    return this.status === 0;
  }

  /** True when the request was aborted by the caller or the timeout. */
  get isAbort(): boolean {
    return this.name === "AbortError";
  }
}

/** Raised when a request exceeds its timeout budget. */
export class TimeoutError extends ApiError {
  constructor(params: { url: string; method: string; timeout_ms: number }) {
    super({
      message: `Request timed out after ${params.timeout_ms}ms: ${params.method} ${params.url}`,
      status: 0,
      statusText: "timeout",
      url: params.url,
      method: params.method,
      body: { timeout_ms: params.timeout_ms },
    });
    this.name = "TimeoutError";
    Object.setPrototypeOf(this, TimeoutError.prototype);
  }
}

// ── Message extraction ──────────────────────────────────────────────────────

/**
 * Pull a human-usable message out of whatever the backend returned.
 * FastAPI uses `detail`; the legacy routes use `error` / `message`.
 */
export function extractErrorMessage(status: number, body: unknown, fallback: string): string {
  if (typeof body === "string" && body.trim()) return body.trim().slice(0, 500);
  if (body && typeof body === "object") {
    const rec = body as Record<string, unknown>;
    for (const key of ["detail", "error", "message", "reason"] as const) {
      const value = rec[key];
      if (typeof value === "string" && value.trim()) return value.trim().slice(0, 500);
      if (Array.isArray(value) && value.length > 0) {
        // FastAPI 422 validation errors: [{loc, msg, type}, ...]
        const first = value[0] as Record<string, unknown> | undefined;
        const msg = first && typeof first.msg === "string" ? first.msg : null;
        const loc = first && Array.isArray(first.loc) ? first.loc.join(".") : null;
        if (msg) return `${loc ? `${loc}: ` : ""}${msg}`.slice(0, 500);
      }
    }
  }
  if (status === 0) return fallback;
  return fallback;
}

/** Statuses worth a single retry: the gateway or the backend was briefly down. */
const RETRYABLE_STATUS = new Set([502, 503, 504]);

// ── Core fetch ───────────────────────────────────────────────────────────────

export interface ApiFetchInit extends Omit<RequestInit, "signal"> {
  /** Caller cancellation, combined with the internal timeout controller. */
  signal?: AbortSignal;
  /** Per-request timeout in ms. Default {@link DEFAULT_TIMEOUT_MS}. */
  timeout_ms?: number;
  /** Disable the single 502/503/504 retry. Default false. */
  noRetry?: boolean;
}

const DEFAULT_TIMEOUT_MS = 120_000;
const RETRY_BACKOFF_MS = 500;

/**
 * Combine the caller's AbortSignal with an internal timeout controller.
 * Aborting either one aborts the request; cleanup detaches both listeners.
 */
function withTimeout(
  signal: AbortSignal | undefined,
  timeoutMs: number,
): { signal: AbortSignal; cleanup: () => void; timedOut: () => boolean } {
  const controller = new AbortController();
  let timedOut = false;

  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  const onExternalAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener("abort", onExternalAbort, { once: true });
  }

  return {
    signal: controller.signal,
    timedOut: () => timedOut,
    cleanup: () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onExternalAbort);
    },
  };
}

function authHeaders(extra?: HeadersInit): Record<string, string> {
  const headers: Record<string, string> = {};
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  const supplied = new Headers(extra);
  supplied.forEach((value, key) => {
    headers[key] = value;
  });
  return headers;
}

function buildUrl(path: string): string {
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(path)) return path; // absolute — pass through
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${suffix}`;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * Typed, abortable fetch against the Signal-OS backend.
 *
 * Always resolves with the parsed body or throws {@link ApiError} /
 * {@link TimeoutError} — backend `detail` / `error` text is preserved verbatim.
 *
 * @param path - Path beginning with `/`, e.g. `/api/health`. Absolute URLs pass through.
 * @param init - Standard `fetch` options plus `signal`, `timeout_ms`, `noRetry`.
 * @returns The response body, parsed as JSON when possible.
 * @throws {ApiError} on any non-2xx response, transport failure or timeout.
 */
export async function apiFetch<T>(path: string, init: ApiFetchInit = {}): Promise<T> {
  warnInsecureBaseOnce();

  const { signal, timeout_ms, noRetry, ...rest } = init;
  const url = buildUrl(path);
  const method = (rest.method ?? "GET").toUpperCase();
  const timeoutMs = timeout_ms ?? DEFAULT_TIMEOUT_MS;
  const maxAttempts = noRetry ? 1 : 2;

  let lastError: ApiError | null = null;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const { signal: signalWithTimeout, cleanup, timedOut } = withTimeout(signal, timeoutMs);
    try {
      const response = await fetch(url, {
        ...rest,
        method,
        signal: signalWithTimeout,
        headers: authHeaders(rest.headers),
      });

      const text = await response.text();
      let body: unknown = text;
      if (text) {
        try {
          body = JSON.parse(text);
        } catch {
          body = text;
        }
      } else {
        body = null;
      }

      if (!response.ok) {
        const message = extractErrorMessage(
          response.status,
          body,
          `${method} ${url} failed: ${response.status} ${response.statusText}`.trim(),
        );
        const error = new ApiError({
          message,
          status: response.status,
          statusText: response.statusText,
          url,
          method,
          body,
        });
        if (RETRYABLE_STATUS.has(response.status) && attempt < maxAttempts) {
          lastError = error;
          await sleep(RETRY_BACKOFF_MS * attempt);
          continue;
        }
        throw error;
      }

      // 204 / empty body on a success status.
      return body as T;
    } catch (err) {
      if (err instanceof ApiError) throw err;

      if (timedOut()) {
        throw new TimeoutError({ url, method, timeout_ms: timeoutMs });
      }
      if (signal?.aborted) {
        throw new ApiError({
          message: `Request aborted by caller: ${method} ${url}`,
          status: 0,
          statusText: "aborted",
          url,
          method,
        });
      }

      const message = err instanceof Error ? err.message : String(err);
      const netError = new ApiError({
        message: `Network error: ${message} (${method} ${url})`,
        status: 0,
        statusText: "network",
        url,
        method,
      });
      if (attempt < maxAttempts) {
        lastError = netError;
        await sleep(RETRY_BACKOFF_MS * attempt);
        continue;
      }
      throw netError;
    } finally {
      cleanup();
    }
  }

  /* c8 ignore next */
  throw lastError ?? new ApiError({
    message: `${method} ${url} failed after ${maxAttempts} attempts`,
    status: 0,
    url,
    method,
  });
}

/** JSON `POST` helper. */
function postJson<T>(path: string, payload: unknown, init: ApiFetchInit = {}): Promise<T> {
  return apiFetch<T>(path, {
    method: "POST",
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers as object) },
    body: JSON.stringify(payload ?? {}),
  });
}

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== null && v !== "",
  );
  if (entries.length === 0) return "";
  const search = new URLSearchParams();
  for (const [k, v] of entries) search.set(k, String(v));
  return `?${search.toString()}`;
}

// ── WebSocket URL derivation ────────────────────────────────────────────────

/**
 * Derive the pipeline WebSocket URL for an investigation.
 *
 * Handles the three real deployments:
 *  - no `NEXT_PUBLIC_API_URL` → same-origin `window.location` (proxied `/ws`);
 *  - explicit origin in env → that origin, http→ws / https→wss;
 *  - SSR / no `window` → falls back to a same-origin relative URL, and the
 *    caller resolves it against `location` in the browser.
 *
 * @param invId - Investigation id from `POST /api/investigate`.
 * @returns Absolute `ws://`/`wss://` URL in the browser, relative path on the server.
 */
export function wsUrl(invId: string): string {
  const id = encodeURIComponent(invId);
  const path = `/ws/pipeline/${id}`;

  if (API_BASE) {
    // Explicit backend origin, possibly behind a path-prefix reverse proxy.
    const wsBase = API_BASE.replace(/^http:/i, "ws:").replace(/^https:/i, "wss:");
    return `${wsBase}${path}`;
  }

  if (typeof window !== "undefined" && window.location) {
    const { protocol, host } = window.location;
    const scheme = protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${host}${path}`;
  }

  return path;
}

// ── Investigations (backend/api/investigate.py, prefix `/api`) ───────────────

/**
 * Kick off an investigation asynchronously; returns the queued record.
 * @param input - Any supported target (email, domain, IP, wallet, URL, media...).
 * @param opts - Case binding, forced input type, deep mode, language, abort.
 * @returns The investigation record with `status: "queued" | "routing"`.
 * @throws {ApiError} when the backend rejects the request.
 */
export async function submitInvestigation(
  input: string,
  opts: SubmitInvestigationOptions = {},
): Promise<Investigation> {
  const { signal, timeout_ms, case_id, input_type, deep, lang } = opts;
  return postJson<Investigation>(
    "/api/investigate",
    { input, case_id, input_type, deep, lang },
    { signal, timeout_ms },
  );
}

/**
 * Run an investigation synchronously and return the finished record.
 * @param input - Any supported target.
 * @param opts - Same options as {@link submitInvestigation}.
 * @returns The completed investigation including `report`.
 * @throws {ApiError} on failure; slow pipelines need the 120s default timeout.
 */
export async function submitInvestigationSync(
  input: string,
  opts: SubmitInvestigationOptions = {},
): Promise<Investigation> {
  const { signal, timeout_ms, case_id, input_type, deep, lang } = opts;
  return postJson<Investigation>(
    "/api/investigate-sync",
    { input, case_id, input_type, deep, lang },
    { signal, timeout_ms },
  );
}

/**
 * Fetch the current state of one investigation.
 * @param invId - Investigation id.
 * @param init - Abort signal / timeout override.
 * @returns The stored investigation record.
 * @throws {ApiError} when the id is unknown or invalid.
 */
export function getInvestigation(invId: string, init: ApiFetchInit = {}): Promise<Investigation> {
  return apiFetch<Investigation>(`/api/investigate/${encodeURIComponent(invId)}`, init);
}

/**
 * List investigations, newest first.
 * @param params - `case_id` filter and `limit` page size.
 * @param init - Abort signal / timeout override.
 * @returns An array of investigations (backend returns a bare list).
 * @throws {ApiError} on transport or HTTP failure.
 */
export async function listInvestigations(
  params: { case_id?: string; limit?: number } = {},
  init: ApiFetchInit = {},
): Promise<Investigation[]> {
  const raw = await apiFetch<Investigation[] | Paged<Investigation>>(
    `/api/investigations${qs({ case_id: params.case_id, limit: params.limit })}`,
    init,
  );
  return unwrapList<Investigation>(raw);
}

/**
 * Free-form OSINT search across the connector swarm.
 * @param req - Query plus optional case binding, deep mode and language.
 * @returns Search results with any extracted entities and signals.
 * @throws {ApiError} when the query is invalid or the backend fails.
 */
export function runSearch(req: SearchRequest): Promise<SearchResponse> {
  const { query, ...opts } = req;
  const { signal, timeout_ms, ...rest } = opts;
  return postJson<SearchResponse>("/api/search", { query, ...rest }, { signal, timeout_ms });
}

/**
 * Reverse-search an uploaded photo, video or audio clip.
 * @param file - Blob plus filename; sent as multipart/form-data.
 * @param opts - Case binding, deep mode, language, abort, timeout.
 * @returns Media analysis output.
 * @throws {ApiError} when the upload is rejected (type, size, or backend error).
 */
export function submitPhotoSearch(
  file: Blob,
  opts: SearchOptions & { filename?: string } = {},
): Promise<PhotoSearchResult> {
  const { signal, timeout_ms, filename, ...rest } = opts;
  const form = new FormData();
  form.append(
    "file",
    file,
    filename ?? (file instanceof File ? file.name : "upload"),
  );
  for (const [key, value] of Object.entries(rest)) {
    if (value !== undefined && value !== null) form.append(key, String(value));
  }
  return apiFetch<PhotoSearchResult>("/api/photo-search", {
    method: "POST",
    body: form,
    signal,
    timeout_ms,
  });
}

/**
 * Person-name OSINT sweep.
 * @param req - Name plus optional source restrictions and case binding.
 * @returns Results for the name across the requested surfaces.
 * @throws {ApiError} when the name is missing or the backend fails.
 */
export function submitNameSearch(req: NameSearchRequest): Promise<SearchResponse> {
  const { name, ...opts } = req;
  const { signal, timeout_ms, ...rest } = opts;
  return postJson<SearchResponse>("/api/name-search", { name, ...rest }, { signal, timeout_ms });
}

// ── Cases (backend/api/cases.py, prefix `/api/cases`) ────────────────────────

function unwrapList<T>(raw: unknown): T[] {
  if (Array.isArray(raw)) return raw as T[];
  if (raw && typeof raw === "object") {
    const rec = raw as Record<string, unknown>;
    for (const key of ["items", "results", "data"]) {
      const value = rec[key];
      if (Array.isArray(value)) return value as T[];
    }
  }
  return [];
}

/**
 * List cases.
 * @param params - `limit` page size.
 * @param init - Abort signal / timeout override.
 * @returns Cases, newest first.
 * @throws {ApiError} on transport or HTTP failure.
 */
export async function listCases(
  params: { limit?: number } = {},
  init: ApiFetchInit = {},
): Promise<Case[]> {
  const raw = await apiFetch<Case[] | Paged<Case>>(`/api/cases${qs({ limit: params.limit })}`, init);
  return unwrapList<Case>(raw);
}

/**
 * Create a case.
 * @param payload - `name` plus optional target, tags, notes, case_id.
 * @param init - Abort signal / timeout override.
 * @returns The stored case including its generated `case_id`.
 * @throws {ApiError} on validation failure or backend error.
 */
export function createCase(
  payload: { name: string; target?: string; tags?: string[]; notes?: string; case_id?: string },
  init: ApiFetchInit = {},
): Promise<Case> {
  return postJson<Case>("/api/cases", payload, init);
}

/**
 * Fetch one case.
 * @param caseId - Case id.
 * @param init - Abort signal / timeout override.
 * @returns The case record.
 * @throws {ApiError} when the case does not exist.
 */
export function getCase(caseId: string, init: ApiFetchInit = {}): Promise<Case> {
  return apiFetch<Case>(`/api/cases/${encodeURIComponent(caseId)}`, init);
}

/**
 * Patch a case.
 * @param caseId - Case id.
 * @param patch - Any subset of name, target, tags, notes.
 * @param init - Abort signal / timeout override.
 * @returns The updated case.
 * @throws {ApiError} on validation failure or unknown case.
 */
export function updateCase(
  caseId: string,
  patch: Partial<Pick<Case, "name" | "target" | "tags" | "notes">>,
  init: ApiFetchInit = {},
): Promise<Case> {
  return apiFetch<Case>(`/api/cases/${encodeURIComponent(caseId)}`, {
    method: "PATCH",
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers as object) },
    body: JSON.stringify(patch),
  });
}

/**
 * Delete a case.
 * @param caseId - Case id.
 * @param init - Abort signal / timeout override.
 * @returns `true` once the backend reports the deletion.
 * @throws {ApiError} when the case does not exist or deletion is refused.
 */
export async function deleteCase(caseId: string, init: ApiFetchInit = {}): Promise<boolean> {
  await apiFetch<unknown>(`/api/cases/${encodeURIComponent(caseId)}`, {
    method: "DELETE",
    ...init,
  });
  return true;
}

/**
 * Aggregate statistics for one case.
 * @param caseId - Case id.
 * @param init - Abort signal / timeout override.
 * @returns Entity / signal / agent counts and time bounds.
 * @throws {ApiError} when the case does not exist.
 */
export function getCaseStats(caseId: string, init: ApiFetchInit = {}): Promise<CaseStats> {
  return apiFetch<CaseStats>(`/api/cases/${encodeURIComponent(caseId)}/stats`, init);
}

/**
 * All entities observed across a case's investigations.
 * @param caseId - Case id.
 * @param init - Abort signal / timeout override.
 * @returns The deduplicated entity list.
 * @throws {ApiError} when the case does not exist.
 */
export async function getCaseEntities(
  caseId: string,
  init: ApiFetchInit = {},
): Promise<Entity[]> {
  const raw = await apiFetch<
    Entity[] | Paged<Entity> | { entities?: Entity[] }
  >(`/api/cases/${encodeURIComponent(caseId)}/entities`, init);
  if (Array.isArray(raw)) return raw;
  if (raw && typeof raw === "object") {
    const rec = raw as Record<string, unknown>;
    if (Array.isArray(rec.entities)) return rec.entities as Entity[];
  }
  return unwrapList<Entity>(raw);
}

/**
 * Merged timeline across a case's investigations.
 * @param caseId - Case id.
 * @param init - Abort signal / timeout override.
 * @returns Chronologically ordered timeline events.
 * @throws {ApiError} when the case does not exist.
 */
export async function getCaseTimeline(
  caseId: string,
  init: ApiFetchInit = {},
): Promise<TimelineEvent[]> {
  const raw = await apiFetch<
    TimelineEvent[] | Paged<TimelineEvent> | { timeline?: TimelineEvent[] }
  >(`/api/cases/${encodeURIComponent(caseId)}/timeline`, init);
  if (Array.isArray(raw)) return raw;
  if (raw && typeof raw === "object") {
    const rec = raw as Record<string, unknown>;
    if (Array.isArray(rec.timeline)) return rec.timeline as TimelineEvent[];
  }
  return unwrapList<TimelineEvent>(raw);
}

/**
 * Export a case bundle (JSON of cases + investigations + entities).
 * @param caseId - Case id.
 * @param init - Abort signal / timeout override.
 * @returns The export payload; shape is backend-defined JSON.
 * @throws {ApiError} when the case does not exist or export fails.
 */
export function exportCase(caseId: string, init: ApiFetchInit = {}): Promise<unknown> {
  return apiFetch<unknown>(`/api/cases/${encodeURIComponent(caseId)}/export`, {
    method: "POST",
    ...init,
  });
}

// ── Agents (backend/api/agents.py, prefix `/api`) ────────────────────────────

/**
 * List every registered agent with its descriptor and live status.
 * @param init - Abort signal / timeout override.
 * @returns Agent descriptors (bare array, or unwrapped from `items`/`agents`).
 * @throws {ApiError} on transport or HTTP failure.
 */
export async function listAgents(init: ApiFetchInit = {}): Promise<AgentMeta[]> {
  const raw = await apiFetch<AgentMeta[] | { agents?: AgentMeta[]; items?: AgentMeta[] }>(
    "/api/agents",
    init,
  );
  if (raw && !Array.isArray(raw) && typeof raw === "object") {
    const rec = raw as Record<string, unknown>;
    if (Array.isArray(rec.agents)) return rec.agents as AgentMeta[];
  }
  return unwrapList<AgentMeta>(raw);
}

/**
 * Run a single agent against a target, outside the APEX pipeline.
 * @param name - Registered agent name, e.g. `scout`.
 * @param payload - `input` plus optional context overrides.
 * @param init - Abort signal / timeout override.
 * @returns The agent's result envelope.
 * @throws {ApiError} when the agent is unknown or its run fails.
 */
export function runAgent(
  name: string,
  payload: { input: string; context?: Record<string, unknown> },
  init: ApiFetchInit = {},
): Promise<{ agent?: string; status?: AgentStatus; result?: AgentResult; output?: unknown }> {
  return postJson(
    `/api/agents/${encodeURIComponent(name)}/run`,
    payload,
    init,
  );
}

// ── Ops (backend/api/ops.py, prefix `/api`) ──────────────────────────────────

/**
 * Liveness / dependency health.
 * @param init - Abort signal / timeout override.
 * @returns Health envelope including llm, store and cache state.
 * @throws {ApiError} when the backend is unreachable.
 */
export function getHealth(init: ApiFetchInit = {}): Promise<HealthStatus> {
  return apiFetch<HealthStatus>("/api/health", { timeout_ms: 10_000, ...init });
}

/**
 * Prometheus-compatible counters, timers and gauges.
 * @param init - Abort signal / timeout override.
 * @returns The current metrics snapshot.
 * @throws {ApiError} on transport or HTTP failure.
 */
export function getMetrics(init: ApiFetchInit = {}): Promise<MetricsSnapshot> {
  return apiFetch<MetricsSnapshot>("/api/metrics", init);
}

/**
 * Current opsec circuit state (Tor/VPN rotation status).
 * @param init - Abort signal / timeout override.
 * @returns Whether the circuit is active, plus hop/expiry metadata.
 * @throws {ApiError} on transport or HTTP failure.
 */
export function getOpsecStatus(init: ApiFetchInit = {}): Promise<OpsecStatus> {
  return apiFetch<OpsecStatus>("/api/opsec/status", init);
}

/**
 * Rotate to a fresh circuit (new exit, new identity).
 * @param init - Abort signal / timeout override.
 * @returns The newly created circuit record.
 * @throws {ApiError} when rotation fails or the opsec backend is down.
 */
export function newCircuit(init: ApiFetchInit = {}): Promise<OpsecStatus> {
  return postJson<OpsecStatus>("/api/opsec/newcircuit", {}, init);
}

// ── Reports (backend/api/reports.py, prefix `/api`) ──────────────────────────

/**
 * Fetch a rendered report for an investigation.
 * @param invId - Investigation id.
 * @param format - `markdown` (default), `pdf` or `json`.
 * @param init - Abort signal / timeout override.
 * @returns The report payload; binary PDF comes back as `ArrayBuffer`.
 * @throws {ApiError} when the investigation is unknown or rendering fails.
 */
export function getReport<T = ReportExport | Report | string | ArrayBuffer>(
  invId: string,
  format: ReportFormat = "markdown",
  init: ApiFetchInit = {},
): Promise<T> {
  return apiFetch<T>(
    `/api/investigate/${encodeURIComponent(invId)}/report${qs({ format })}`,
    init,
  );
}

/**
 * Fetch the raw export artefact for an investigation.
 * @param invId - Investigation id.
 * @param format - `markdown`, `pdf` or `json`.
 * @param init - Abort signal / timeout override.
 * @returns An `ArrayBuffer` suitable for `URL.createObjectURL`.
 * @throws {ApiError} when the investigation is unknown or export fails.
 */
export function downloadReport(
  invId: string,
  format: ReportFormat = "pdf",
  init: ApiFetchInit = {},
): Promise<ArrayBuffer> {
  return apiFetch<ArrayBuffer>(
    `/api/investigate/${encodeURIComponent(invId)}/export${qs({ format })}`,
    { ...init, headers: { Accept: "application/octet-stream", ...(init.headers as object) } },
  );
}

export default apiFetch;