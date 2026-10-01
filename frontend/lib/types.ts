/**
 * types.ts — canonical Signal-OS frontend type surface.
 *
 * Shapes are pinned by `docs/CONTRACTS.md` §11 (WS-FE-*) and §3 (entity /
 * signal / graph / timeline). Every other frontend module imports from here so
 * there is exactly one definition of each wire shape.
 *
 * This module is types-only on purpose: no runtime imports, no side effects,
 * safe to pull into server components, client components and tests alike.
 */

// ── Entities & signals (CONTRACTS.md §11) ───────────────────────────────────

/** Canonical entity taxonomy. The backend may not invent types outside this union. */
export type EntityType =
  | "person"
  | "email"
  | "username"
  | "domain"
  | "url"
  | "ip"
  | "phone"
  | "account"
  | "image"
  | "face"
  | "location"
  | "wallet"
  | "org"
  | "crypto"
  | "document"
  | "note"
  | "other";

export interface Entity {
  /** Stable, deterministic: `${type}:${normalized_value}`. */
  id: string;
  label: string;
  type: EntityType;
  value?: string;
  /** 0..1 */
  confidence?: number;
  /** Agent name that produced the entity. */
  source?: string;
  /** ISO 8601 */
  first_seen?: string;
  /** ISO 8601 */
  last_seen?: string;
  attrs?: Record<string, unknown>;
}

export interface Signal {
  /** e.g. account_found, breach, geolocation, dork, ... */
  type: string;
  value: unknown;
  /** Agent / connector name. */
  source: string;
  /** 0..1 — always present. */
  confidence: number;
  /** ISO 8601 when the signal carries a date. */
  ts?: string | null;
  url?: string | null;
  attrs?: Record<string, unknown>;
}

export interface GraphNode {
  id: string;
  label: string;
  type: EntityType;
  /** Relative importance, used for node sizing. */
  weight?: number;
  confidence?: number;
  /** Free-form cluster/group label. */
  group?: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  label?: string;
  type?: string;
  weight?: number;
  confidence?: number;
}

/** A named cluster of nodes (contract §3 `clusters`). */
export interface GraphCluster {
  id: string;
  label: string;
  size: number;
  members: string[];
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  clusters?: GraphCluster[];
}

export interface TimelineEvent {
  id: string;
  /** ISO 8601 instant. */
  ts?: string;
  /** Human-facing date (may be coarser than `ts`). */
  date: string;
  label: string;
  description?: string;
  source?: string;
  confidence?: number;
  /** Entity ids referenced by this event. */
  entities?: string[];
}

// ── Agents ──────────────────────────────────────────────────────────────────

/** Terminal state of a single agent run (mirrors `backend.agents.base.AgentResult`). */
export type AgentStatus = "done" | "error" | "partial" | "skipped";

/** Lifecycle as surfaced by the pipeline trace / status bar. */
export type InvestigationStatus = "idle" | "routing" | "running" | "done" | "error";

export interface AgentResult {
  agent: string;
  role?: string;
  icon?: string;
  status: AgentStatus;
  output?: unknown;
  confidence?: number;
  reasoning?: string;
  entities_found?: Entity[];
  signals?: Signal[];
  latency_s?: number;
  tokens_used?: number;
  error?: string;
  /** Ordered step log emitted by the agent during its run. */
  steps?: string[];
}

/** Static agent descriptor from `GET /api/agents`. */
export interface AgentMeta {
  name: string;
  role: string;
  icon: string;
  description: string;
  status: AgentStatus | "idle" | "running";
}

/** Node of the agent registry graph (`GET /api/agents/registry/graph`). */
export interface AgentNode {
  name: string;
  role?: string;
  icon?: string;
  /** Names of agents this one depends on. */
  depends_on?: string[];
  inputs?: string[];
  outputs?: string[];
}

// ── Reports & investigations ────────────────────────────────────────────────

export interface Report {
  summary: string;
  input_type?: string;
  agents_activated?: string[];
  entities?: Entity[];
  signals?: Signal[];
  graph?: Graph;
  timeline?: TimelineEvent[];
  markdown?: string;
  /** 0..1 */
  confidence?: number;
  latency_s?: number;
}

export interface Investigation {
  inv_id: string;
  case_id?: string;
  input: string;
  input_type: string;
  status: InvestigationStatus;
  /** Ordered pipeline trace lines. */
  steps?: string[];
  agents?: AgentResult[];
  report?: Report;
  /** ISO 8601 */
  started_at?: string;
  /** ISO 8601 */
  ended_at?: string;
  error?: string;
  agents_activated?: string[];
  routing_confidence?: number;
  routing_reasoning?: string;
  /** Present while a run is live; the stream closes after this arrives. */
  entities?: Graph;
}

// ── Cases ───────────────────────────────────────────────────────────────────

export interface Case {
  case_id: string;
  name: string;
  target?: string;
  created_at?: string;
  updated_at?: string;
  investigation_count?: number;
  tags?: string[];
  notes?: string;
}

export interface CaseStats {
  case_id: string;
  investigation_count: number;
  entity_count: number;
  signal_count: number;
  agent_count: number;
  first_seen?: string;
  last_seen?: string;
  entity_types?: Partial<Record<EntityType, number>>;
}

// ── Generic API envelopes ───────────────────────────────────────────────────

/** Uniform pagination envelope returned by list endpoints. */
export interface Paged<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type HealthLevel = "ok" | "degraded" | "down";

export interface HealthStatus {
  status: HealthLevel;
  version?: string;
  uptime_s?: number;
  llm?: { available: boolean; provider?: string; model?: string; error?: string };
  store?: { backend: string; degraded?: boolean; error?: string };
  cache?: { backend: string; hit_rate?: number; entries?: number };
  agents?: { total: number; healthy: number };
  checks?: Record<string, { ok: boolean; detail?: string }>;
}

export interface MetricsSnapshot {
  counters?: Record<string, number>;
  timings?: Record<string, { count: number; total_s: number; avg_s: number; max_s?: number }>;
  gauges?: Record<string, number>;
  uptime_s?: number;
}

export interface OpsecStatus {
  /** True when traffic is routed through the active circuit. */
  active: boolean;
  circuit_id?: string;
  circuit_label?: string;
  /** Fraction of traffic already through the circuit, 0..1. */
  coverage?: number;
  hops?: number;
  started_at?: string;
  expires_at?: string;
  circuit?: {
    id: string;
    label?: string;
    hops?: number;
    created_at?: string;
    expires_at?: string;
    status?: string;
  };
}

// ── WebSocket pipeline events ───────────────────────────────────────────────

/**
 * Events pushed over `WS /ws/pipeline/{inv_id}`. The backend emits
 * `step` (a trace line), `agent` (per-agent lifecycle), `done` (final state,
 * replayed when subscribing to an already-finished run) and `error`.
 */
export type PipelineEventType = "step" | "agent" | "done" | "error" | "ping";

export interface PipelineEventBase {
  type: PipelineEventType;
  inv_id?: string;
  /** ISO 8601 server timestamp when supplied. */
  ts?: string;
}

export interface StepEvent extends PipelineEventBase {
  type: "step";
  message: string;
}

export interface AgentEvent extends PipelineEventBase {
  type: "agent";
  agent: string;
  status: AgentStatus;
  output?: unknown;
  confidence?: number;
  error?: string;
  latency_s?: number;
}

/** Terminal event. Carries the complete final state so a late subscriber can render. */
export interface DoneEvent extends PipelineEventBase {
  type: "done";
  status?: InvestigationStatus;
  report?: Report;
  agents?: AgentResult[];
  entities?: Graph;
  steps?: string[];
  input?: string;
  input_type?: string;
  agents_activated?: string[];
  latency_s?: number;
}

export interface ErrorEvent extends PipelineEventBase {
  type: "error";
  message?: string;
  error?: string;
}

export interface PingEvent extends PipelineEventBase {
  type: "ping";
  /** Echoed back by the client keepalive. */
  nonce?: string | number;
}

export type PipelineEvent = StepEvent | AgentEvent | DoneEvent | ErrorEvent | PingEvent;

// ── Search request / response bodies ────────────────────────────────────────

export interface SubmitInvestigationOptions {
  case_id?: string;
  /** Force a specific input type instead of letting APEX route it. */
  input_type?: string;
  /** Run the deeper connector sweep. */
  deep?: boolean;
  /** Response language hint for LLM-backed agents. */
  lang?: string;
  /** Optional client cancellation. */
  signal?: AbortSignal;
  /** Per-request timeout override in ms (default 120_000). */
  timeout_ms?: number;
}

export interface SearchOptions {
  case_id?: string;
  deep?: boolean;
  lang?: string;
  signal?: AbortSignal;
  timeout_ms?: number;
}

export interface SearchRequest extends SearchOptions {
  query: string;
}

export interface SearchResultItem {
  title?: string;
  url?: string;
  snippet?: string;
  source?: string;
  confidence?: number;
  ts?: string;
}

export interface SearchResponse {
  query: string;
  results: SearchResultItem[];
  entities?: Entity[];
  signals?: Signal[];
  markdown?: string;
}

export interface NameSearchRequest extends SearchOptions {
  name: string;
  /** Restrict OSINT surface, e.g. ["social","breach"]. */
  sources?: string[];
}

export interface PhotoSearchResult {
  /** Object-storage / CDN URL of the analysed media. */
  media_url?: string;
  input_type?: string;
  entities?: Entity[];
  signals?: Signal[];
  markdown?: string;
  report?: Report;
}

export type ReportFormat = "markdown" | "pdf" | "json";

export interface ReportExport {
  inv_id: string;
  format: ReportFormat;
  /** Present for markdown/json; PDF returns binary from `downloadReport`. */
  content?: string;
  url?: string;
  generated_at?: string;
}