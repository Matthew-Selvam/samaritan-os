/**
 * views.ts — the Signal-OS view registry and the shell-level types that only
 * this frontend owns.
 *
 * The registry is the single source of truth for: navigation order, the left
 * rail, the ⌘K palette entries, the 1-9 hotkeys and the status bar. Views are
 * client components but this module is pure data so it can be imported by
 * anything without dragging React along.
 *
 * NOTE: symbols the shared lib files (types.ts / api.ts / entityTypes.ts /
 * format.ts) do not export are declared here and *only* here, so there is
 * still exactly one definition of each:
 *   - `ViewId` / `VIEWS` / view lookup helpers
 *   - `DeepHealth` (GET /api/health/deep is not modelled in lib/types.ts)
 *   - `DashboardStats` + the metric-derivation helpers
 */

import type { HealthLevel } from "./types";
import type { EntityType } from "./entityTypes";

/* ── View registry ──────────────────────────────────────────────────────────── */

/** Every destination in the shell. The order is also the ⌘K / 1-9 order. */
export type ViewId =
  | "dashboard"
  | "investigate"
  | "cases"
  | "agents"
  | "graph"
  | "timeline"
  | "knowledge"
  | "reports"
  | "ops";

/** A navigable destination. */
export interface ViewDef {
  readonly id: ViewId;
  /** Uppercase label used in the top bar and status bar. */
  readonly label: string;
  /** Rail tooltip + palette hint. */
  readonly description: string;
  /** Single-key shortcut digit (`1`..`9`), matching the rail order. */
  readonly hotkey: string;
  /** Lucide glyph name — resolved in the shell so this module stays pure. */
  readonly icon: string;
  /** Extra palette search terms. */
  readonly keywords: readonly string[];
}

/**
 * The registry. The order is also the rail order and the digit hotkeys.
 *
 * Digits 1-3 are deliberately GRAPH / TIMELINE / REPORTS: those three bindings
 * already exist in `lib/shortcuts.ts` (`view.graph`, `view.timeline`,
 * `view.reports`) and are rendered in the shortcuts dialog. Starting here at 1
 * keeps one key meaning exactly one thing — re-numbering them would make the
 * dialog lie. The remaining six views take 4-9.
 */
export const VIEWS: readonly ViewDef[] = [
  {
    id: "graph",
    label: "GRAPH",
    description: "Force-directed entity graph with clustering and export.",
    hotkey: "1",
    icon: "Network",
    keywords: ["entity", "network", "links", "cytoscape", "clusters"],
  },
  {
    id: "timeline",
    label: "TIMELINE",
    description: "KRONOS chronology: zoom, filter, cross-link to graph.",
    hotkey: "2",
    icon: "Clock",
    keywords: ["kronos", "chronology", "events", "dates"],
  },
  {
    id: "reports",
    label: "REPORTS",
    description: "Rendered intelligence reports with export and print.",
    hotkey: "3",
    icon: "FileText",
    keywords: ["quill", "markdown", "export", "pdf", "brief"],
  },
  {
    id: "dashboard",
    label: "DASHBOARD",
    description: "Mission screen: live stats, service health, recent runs.",
    hotkey: "4",
    icon: "LayoutDashboard",
    keywords: ["home", "mission", "overview", "stats", "health"],
  },
  {
    id: "investigate",
    label: "INVESTIGATE",
    description: "Deep work: live pipeline, agents, entities, signals.",
    hotkey: "5",
    icon: "Crosshair",
    keywords: ["run", "search", "pipeline", "live", "work"],
  },
  {
    id: "cases",
    label: "CASES",
    description: "Case management: create, rename, tag, export.",
    hotkey: "6",
    icon: "FolderKanban",
    keywords: ["case", "workspace", "files", "manage"],
  },
  {
    id: "agents",
    label: "AGENTS",
    description: "The 16-agent roster, routing DAG and console.",
    hotkey: "7",
    icon: "Bot",
    keywords: ["roster", "swarm", "dag", "run agent", "routing"],
  },
  {
    id: "knowledge",
    label: "KNOWLEDGE",
    description: "Cross-case vault: saved entities, notes, recall, purge.",
    hotkey: "8",
    icon: "Database",
    keywords: ["vault", "memory", "saved", "notes", "gdpr", "recall"],
  },
  {
    id: "ops",
    label: "OPS",
    description: "OPSEC, circuit rotation, config, connector health.",
    hotkey: "9",
    icon: "ShieldCheck",
    keywords: ["opsec", "tor", "circuit", "config", "connectors", "metrics"],
  },
] as const;

/** View shown on first paint. */
export const DEFAULT_VIEW: ViewId = "dashboard";

const VIEW_INDEX: ReadonlyMap<ViewId, ViewDef> = new Map(VIEWS.map((v) => [v.id, v]));

/** Look up a view definition. */
export function viewById(id: ViewId | string | null | undefined): ViewDef | undefined {
  if (!id) return undefined;
  return VIEW_INDEX.get(id as ViewId);
}

/** Guard for values arriving from storage, URL or the palette. */
export function isViewId(value: unknown): value is ViewId {
  return typeof value === "string" && VIEW_INDEX.has(value as ViewId);
}

/** Resolve a 1-9 hotkey digit to its view. */
export function viewByHotkey(digit: string): ViewDef | undefined {
  return VIEWS.find((v) => v.hotkey === digit);
}

/* ── Deep health (GET /api/health/deep) ─────────────────────────────────────── */

/** One dependency probe from `/api/health/deep`. */
export interface HealthProbe {
  ok: boolean;
  detail?: string;
  latency_ms?: number;
}

/**
 * Response of `GET /api/health/deep`, plus the lighter `/api/health` shape.
 * The backend has shipped several generations of this payload, so every field
 * is optional and {@link normalizeDeepHealth} folds unknown shapes into this one.
 */
export interface DeepHealth {
  status: HealthLevel;
  version?: string;
  uptime_s?: number;
  llm?: { available?: boolean; provider?: string; model?: string; error?: string };
  store?: { backend?: string; degraded?: boolean; error?: string };
  cache?: { backend?: string; hit_rate?: number; entries?: number };
  agents?: { total?: number; healthy?: number };
  /** Free-form probe map keyed by dependency name. */
  checks?: Record<string, HealthProbe>;
  /** Alias some deployments use for the same map. */
  services?: Record<string, HealthProbe>;
}

/** The service grid the dashboard and OPS view render, in display order. */
export const SERVICE_KEYS = [
  "postgres",
  "redis",
  "neo4j",
  "qdrant",
  "minio",
  "llm",
  "opsec",
] as const;

export type ServiceKey = (typeof SERVICE_KEYS)[number];

/** Display metadata for the service-health grid. */
export const SERVICE_META: Readonly<Record<ServiceKey, { label: string; hint: string }>> = {
  postgres: { label: "Postgres", hint: "Relational store" },
  redis: { label: "Redis", hint: "Cache / broker" },
  neo4j: { label: "Neo4j", hint: "Graph store" },
  qdrant: { label: "Qdrant", hint: "Vector store" },
  minio: { label: "MinIO", hint: "Object storage" },
  llm: { label: "LLM", hint: "Model provider" },
  opsec: { label: "OPSEC", hint: "Tor / circuit" },
};

function asProbe(value: unknown): HealthProbe | null {
  if (value === true) return { ok: true };
  if (value === false || value === null || value === undefined) return null;
  if (typeof value === "object") {
    const rec = value as Record<string, unknown>;
    if (typeof rec.ok === "boolean") {
      return {
        ok: rec.ok,
        detail: typeof rec.detail === "string" ? rec.detail : undefined,
        latency_ms: typeof rec.latency_ms === "number" ? rec.latency_ms : undefined,
      };
    }
    if (typeof rec.status === "string") {
      const ok = rec.status === "ok" || rec.status === "up" || rec.status === "healthy";
      return {
        ok,
        detail: typeof rec.detail === "string" ? rec.detail : rec.status,
        latency_ms: typeof rec.latency_ms === "number" ? rec.latency_ms : undefined,
      };
    }
  }
  if (typeof value === "number") return { ok: value > 0, detail: `score ${value}` };
  return null;
}

function asLevel(value: unknown): HealthLevel {
  if (value === "ok" || value === "degraded" || value === "down") return value;
  return "degraded";
}

/**
 * Fold any `/api/health` or `/api/health/deep` payload into {@link DeepHealth}.
 * Never throws: a non-object body degrades to `{ status: "degraded" }`.
 */
export function normalizeDeepHealth(raw: unknown): DeepHealth {
  if (!raw || typeof raw !== "object") return { status: "degraded" };
  const rec = raw as Record<string, unknown>;
  const checks: Record<string, HealthProbe> = {};
  for (const key of ["checks", "services", "dependencies"] as const) {
    const block = rec[key];
    if (block && typeof block === "object") {
      for (const [name, value] of Object.entries(block as Record<string, unknown>)) {
        const probe = asProbe(value);
        if (probe) checks[name.toLowerCase()] = probe;
      }
    }
  }
  const out: DeepHealth = {
    status: asLevel(rec.status),
    version: typeof rec.version === "string" ? rec.version : undefined,
    uptime_s: typeof rec.uptime_s === "number" ? rec.uptime_s : undefined,
    checks: Object.keys(checks).length > 0 ? checks : undefined,
  };
  if (rec.llm && typeof rec.llm === "object") out.llm = rec.llm as DeepHealth["llm"];
  if (rec.store && typeof rec.store === "object") out.store = rec.store as DeepHealth["store"];
  if (rec.cache && typeof rec.cache === "object") out.cache = rec.cache as DeepHealth["cache"];
  if (rec.agents && typeof rec.agents === "object") {
    const agents = rec.agents as Record<string, unknown>;
    out.agents = {
      total: typeof agents.total === "number" ? agents.total : undefined,
      healthy: typeof agents.healthy === "number" ? agents.healthy : undefined,
    };
  }
  return out;
}

/**
 * Resolve one service row for the health grid.
 * @returns `null` when the backend did not report the dependency at all, so the
 *          grid can render "not reported" instead of a misleading green dot.
 */
export function probeFor(
  health: DeepHealth | null,
  key: ServiceKey,
): HealthProbe | null {
  if (!health) return null;
  if (key === "llm" && health.llm) {
    return {
      ok: health.llm.available !== false,
      detail: health.llm.model ?? health.llm.provider ?? undefined,
    };
  }
  const checks = health.checks ?? health.services;
  if (!checks) return null;
  const probe = checks[key];
  if (probe) return probe;
  // Tolerate `postgresql`, `min_io`, `tor`, `vector_store`, ... aliases.
  const alias: Record<ServiceKey, readonly string[]> = {
    postgres: ["postgresql", "postgres", "db", "sql"],
    redis: ["redis", "cache"],
    neo4j: ["neo4j", "graph"],
    qdrant: ["qdrant", "vector", "vectors", "vector_store"],
    minio: ["minio", "object_storage", "s3", "storage"],
    llm: ["llm", "model", "vllm", "ollama"],
    opsec: ["opsec", "tor", "circuit"],
  };
  for (const name of alias[key]) {
    const found = checks[name];
    if (found) return found;
  }
  return null;
}

/* ── Derived dashboard statistics ───────────────────────────────────────────── */

/** The six headline numbers on the mission screen. */
export interface DashboardStats {
  investigationsRun: number;
  agentsActive: number;
  entitiesDiscovered: number;
  signalsCollected: number;
  avgLatencyMs: number | null;
  errorRatePct: number | null;
}

/** Raw inputs to {@link deriveStats}. */
export interface StatsInput {
  /** `/api/metrics` counters / timings / gauges, when available. */
  counters?: Record<string, number>;
  timings?: Record<string, { count?: number; total_s?: number; avg_s?: number; max_s?: number }>;
  gauges?: Record<string, number>;
  /** Investigation history, used when metrics carry no counters. */
  investigations?: number;
  /** Agents currently mid-run. */
  activeAgents?: number;
  /** Entity + signal counts for the selected investigation. */
  entities?: number;
  signals?: number;
}

/** First counter whose key matches one of `names` (case-insensitive). */
function pickCounter(counters: Record<string, number> | undefined, names: readonly string[]): number {
  if (!counters) return 0;
  for (const name of names) {
    const hit = counters[name];
    if (typeof hit === "number" && Number.isFinite(hit)) return hit;
  }
  return 0;
}

/**
 * Derive the six headline stats from whatever the backend actually returned.
 *
 * Every field degrades to a real number rather than `NaN`, so the stat tiles can
 * render unconditionally — a `null` only appears where "we do not know" is the
 * honest answer (latency / error rate with no data behind them).
 */
export function deriveStats(input: StatsInput): DashboardStats {
  const counters = input.counters;
  const gauges = input.gauges;

  const investigationsRun =
    pickCounter(counters, [
      "investigations_total",
      "investigations",
      "investigation_total",
      "runs_total",
      "runs",
    ]) ||
    (typeof input.investigations === "number" ? input.investigations : 0);

  const agentsActive =
    (typeof gauges?.agents_active === "number" ? gauges.agents_active : undefined) ??
    pickCounter(counters, ["agents_active", "agents_running"]) ??
    (typeof input.activeAgents === "number" ? input.activeAgents : 0);

  const entitiesDiscovered =
    pickCounter(counters, ["entities_total", "entities", "entity_total"]) ||
    (typeof input.entities === "number" ? input.entities : 0);

  const signalsCollected =
    pickCounter(counters, ["signals_total", "signals", "signal_total"]) ||
    (typeof input.signals === "number" ? input.signals : 0);

  let avgLatencyMs: number | null = null;
  const timing =
    input.timings?.["investigate"] ??
    input.timings?.["investigation"] ??
    input.timings?.["pipeline"] ??
    Object.values(input.timings ?? {})[0];
  if (timing) {
    const avgS = typeof timing.avg_s === "number" ? timing.avg_s : null;
    const count = typeof timing.count === "number" ? timing.count : 0;
    if (avgS !== null && count > 0) avgLatencyMs = avgS * 1000;
    else if (typeof timing.total_s === "number" && count > 0) {
      avgLatencyMs = (timing.total_s / count) * 1000;
    }
  }
  if (avgLatencyMs === null && typeof gauges?.avg_latency_s === "number") {
    avgLatencyMs = gauges.avg_latency_s * 1000;
  }

  const errors = pickCounter(counters, ["errors_total", "errors", "error_total"]);
  const total = pickCounter(counters, [
    "investigations_total",
    "requests_total",
    "requests",
  ]);
  const errorRatePct =
    total > 0 && Number.isFinite(errors) ? (errors / total) * 100 : null;

  return {
    investigationsRun,
    agentsActive,
    entitiesDiscovered,
    signalsCollected,
    avgLatencyMs,
    errorRatePct,
  };
}

/* ── Signal / entity tallies ────────────────────────────────────────────────── */

/** One row of the signal-tally breakdown. */
export interface TallyRow {
  key: string;
  label: string;
  color: string;
  count: number;
  share: number;
}

/**
 * Count entities per canonical type and return display rows sorted by count.
 * @param entities - Any list carrying a `type` field.
 * @param colorFor - Resolves a canonical type to its shared hex colour.
 */
export function tallyByEntityType(
  entities: readonly { type?: EntityType | string }[],
  colorFor: (type: string) => string,
): TallyRow[] {
  const counts = new Map<string, number>();
  for (const entity of entities) {
    const key = (entity.type ?? "other") as string;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const total = entities.length;
  return [...counts.entries()]
    .map(([key, count]) => ({
      key,
      label: key,
      color: colorFor(key),
      count,
      share: total > 0 ? (count / total) * 100 : 0,
    }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

/**
 * Count signals per source agent / connector.
 * @param signals - Any list carrying a `source` field.
 */
export function tallyBySource(signals: readonly { source?: string }[]): TallyRow[] {
  const counts = new Map<string, number>();
  for (const signal of signals) {
    const key = (signal.source ?? "unknown").trim() || "unknown";
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const total = signals.length;
  return [...counts.entries()]
    .map(([key, count]) => ({
      key,
      label: key,
      color: "#00d4ff",
      count,
      share: total > 0 ? (count / total) * 100 : 0,
    }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}
