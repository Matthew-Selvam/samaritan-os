/**
 * hooks.ts — small data-fetching primitives shared by every view.
 *
 * Deliberately framework-free: they own the parts that are easy to get wrong
 * (abort on unmount, no setState after unmount, explicit loading/error/empty
 * states, manual refresh without duplicating fetches).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

/** Lifecycle of an async resource. */
export type ResourceState = "idle" | "loading" | "success" | "error";

/** Result of {@link useAsyncResource}. */
export interface Resource<T> {
  data: T | null;
  state: ResourceState;
  error: string | null;
  /** True while a *background* refresh runs over existing data. */
  refreshing: boolean;
  /** `true` when the last successful response had no rows. */
  empty: boolean;
  reload: () => void;
  /** Replace the cached value without a round trip. */
  set: (value: T | null) => void;
}

/** Loader contract: receives an abort signal, returns the parsed value. */
export type Loader<T> = (signal: AbortSignal) => Promise<T>;

export interface AsyncResourceOptions {
  /** Skip fetching until true (e.g. no case selected yet). */
  enabled?: boolean;
  /** Re-fetch on this interval. `0` disables polling. */
  pollMs?: number;
  /** Re-fetch whenever any of these change. */
  deps?: readonly unknown[];
}

/**
 * Fetch a resource, with abort, optional polling and a manual `reload`.
 *
 * Errors are stored as a human-readable string (`Error.message`, or
 * `String(err)` for a thrown non-Error), never as an `any`.
 */
export function useAsyncResource<T>(
  loader: Loader<T>,
  options: AsyncResourceOptions = {},
): Resource<T> {
  const { enabled = true, pollMs = 0, deps = [] } = options;

  const [data, setData] = useState<T | null>(null);
  const [state, setState] = useState<ResourceState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [nonce, setNonce] = useState(0);

  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      setState("idle");
      return;
    }
    const controller = new AbortController();
    let cancelled = false;

    const run = async () => {
      setState((prev) => (prev === "success" ? prev : "loading"));
      setRefreshing(true);
      try {
        const value = await loaderRef.current(controller.signal);
        if (cancelled || !mountedRef.current) return;
        setData(value);
        setError(null);
        setState("success");
      } catch (err) {
        if (cancelled || !mountedRef.current) return;
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
        setState("error");
      } finally {
        if (!cancelled && mountedRef.current) setRefreshing(false);
      }
    };

    void run();
    return () => {
      cancelled = true;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, nonce, ...deps]);

  useEffect(() => {
    if (!enabled || pollMs <= 0) return;
    const id = window.setInterval(() => setNonce((n) => n + 1), pollMs);
    return () => window.clearInterval(id);
  }, [enabled, pollMs]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const set = useCallback((value: T | null) => {
    setData(value);
    setState(value === null ? "idle" : "success");
  }, []);

  return {
    data,
    state,
    error,
    refreshing,
    empty: state === "success" && isEmptyish(data),
    reload,
    set,
  };
}

/**
 * True when a body is HTML rather than a JSON payload.
 *
 * With the API base left same-origin, an unmatched `/api/...` path returns the
 * Next.js SPA document with HTTP 200. Surface that as "backend unreachable"
 * rather than dumping markup into an error panel.
 *
 * @param value - Any value returned by an `apiFetch` call.
 */
export function isHtmlPayload(value: unknown): boolean {
  if (typeof value !== "string") return false;
  const head = value.slice(0, 200).trim().toLowerCase();
  return head.startsWith("<!doctype") || head.startsWith("<html");
}

/** Error thrown when the API base resolves to the SPA document. */
export function missingBackendError(path: string): Error {
  return new Error(
    `No backend at ${path || "/api"} — the request returned the frontend's HTML document. ` +
      `Set NEXT_PUBLIC_API_URL to the FastAPI origin, or add an /api and /ws rewrite in next.config.`,
  );
}

/**
 * Screen a backend call for the "no backend" case.
 *
 * Every view goes through this instead of calling `lib/api.ts` directly, so an
 * HTML body can never reach an error panel. `apiFetch` throws on a non-2xx
 * before the body is inspected, so both the success path and the `ApiError`
 * path are checked.
 *
 * @param call - Performs the request (usually a thin `lib/api.ts` wrapper).
 * @param path - API path used in the error message.
 * @returns The parsed payload.
 * @throws {ApiError} unchanged for genuine API failures.
 * @throws {Error} describing the missing backend when HTML came back.
 */
export async function apiCall<T>(call: () => Promise<T>, path: string): Promise<T> {
  try {
    const value = await call();
    if (isHtmlPayload(value)) throw missingBackendError(path);
    return value;
  } catch (err) {
    if (err instanceof ApiError && isHtmlPayload(err.body)) {
      throw missingBackendError(path);
    }
    throw err;
  }
}

/**
 * {@link apiCall} with the missing-backend case degraded to a fallback value.
 *
 * For data the UI can render without — metrics counters, optional config — a
 * missing backend is not worth an error panel.
 */
export async function apiCallOr<T>(
  call: () => Promise<T>,
  path: string,
  fallback: T,
): Promise<T> {
  try {
    return await apiCall(call, path);
  } catch (err) {
    if (err instanceof ApiError && isHtmlPayload(err.body)) return fallback;
    throw err;
  }
}

/** Heuristic emptiness used for `Resource.empty`. */
function isEmptyish(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") {
    const rec = value as Record<string, unknown>;
    for (const key of ["items", "nodes", "edges", "results", "events", "agents"]) {
      const list = rec[key];
      if (Array.isArray(list)) return list.length === 0;
    }
  }
  return false;
}

/* ── Local storage ──────────────────────────────────────────────────────────── */

/**
 * `useState` mirrored into `localStorage`, tolerant of private-mode failures.
 *
 * @param key - Storage key; the sentinel `""` disables persistence.
 * @param initial - Value used before anything is stored.
 */
export function useLocalStorage<T>(key: string, initial: T): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(() => {
    if (!key || typeof window === "undefined") return initial;
    try {
      const raw = window.localStorage.getItem(key);
      if (!raw) return initial;
      return JSON.parse(raw) as T;
    } catch {
      return initial;
    }
  });

  const set = useCallback(
    (next: T) => {
      setValue(next);
      if (!key) return;
      try {
        window.localStorage.setItem(key, JSON.stringify(next));
      } catch {
        /* quota or private mode — the in-memory value still applies */
      }
    },
    [key],
  );

  return [value, set];
}

/* ── Media query ───────────────────────────────────────────────────────────── */

/**
 * `window.matchMedia` as React state.
 * @param query - Any media query, e.g. `"(min-width: 1440px)"`.
 * @returns `false` during SSR and before the first client effect.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia(query);
    setMatches(mql.matches);
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

/** `true` when the OS asked for reduced motion. */
export function useReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

/* ── Copy to clipboard ──────────────────────────────────────────────────────── */

/** Copy text, resolving `false` when the clipboard is unavailable. */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (typeof navigator === "undefined" || !navigator.clipboard) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Trigger a client-side file download for text or binary payloads. */
export function downloadBlob(
  data: BlobPart,
  filename: string,
  mime = "application/octet-stream",
): void {
  if (typeof document === "undefined") return;
  const blob = data instanceof Blob ? data : new Blob([data], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  // Revoke on the next tick so Safari has time to start the download.
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** JSON pretty-printer used by every export in the app. */
export function toJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
