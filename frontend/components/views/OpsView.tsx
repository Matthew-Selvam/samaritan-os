"use client";

/**
 * OpsView.tsx — the operations console.
 *
 * OPSEC status and circuit rotation, the redacted runtime configuration, the
 * metrics snapshot and a connector-health matrix. Secrets are redacted in the
 * view itself: nothing that looks like a key, token or password is ever
 * rendered, whatever the backend returns.
 */

import { useCallback, useMemo, useState } from "react";
import clsx from "clsx";
import {
  Badge,
  Button,
  DefRow,
  EmptyState,
  ErrorState,
  Meter,
  Panel,
  SkeletonGrid,
  SkeletonRows,
  StatTile,
  Table,
  Tabs,
  useToast,
  type Column,
} from "@/components/ui";
import { OpsecIndicator, type OpsecView } from "@/components/OpsecIndicator";
import { StatusDot } from "@/components/StatusDot";
import { API_BASE, apiFetch, getHealth, getMetrics, newCircuit } from "@/lib/api";
import { formatBytes, formatDuration, formatRelativeTime, truncate, type Tone } from "@/lib/format";
import type { HealthLevel, MetricsSnapshot } from "@/lib/types";
import { normalizeDeepHealth, type DeepHealth } from "@/lib/views";
import { apiCall, apiCallOr, useAsyncResource, useLocalStorage } from "@/lib/hooks";

type OpsTab = "opsec" | "metrics" | "config" | "connectors";

export interface OpsViewProps {
  className?: string;
}

/** Config keys whose values must never be displayed. */
const SECRET_KEY = /(pass|secret|token|key|credential|cookie|auth|salt|private)/i;

/** Values shorter than this are treated as opaque secrets too. */
const MIN_SECRET_LENGTH = 12;

/**
 * Redact one config value.
 * @returns The original value when it is clearly safe, otherwise a mask.
 */
export function redactConfigValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  if (typeof value === "object") {
    try {
      return SECRET_KEY.test(key) ? "«redacted»" : JSON.stringify(value).slice(0, 200);
    } catch {
      return "«unserialisable»";
    }
  }
  const text = String(value);
  if (SECRET_KEY.test(key)) return "«redacted»";
  if (text.length >= MIN_SECRET_LENGTH && /^[A-Za-z0-9_\-+/=.]{16,}$/.test(text)) {
    return "«redacted»";
  }
  return truncate(text, 80);
}

/** Connector row in the health matrix. */
interface ConnectorRow {
  name: string;
  ok: boolean | null;
  detail?: string;
  latency_ms?: number;
}

/**
 * Operations console.
 *
 * @example
 * <OpsView />
 */
export function OpsView({ className }: OpsViewProps) {
  const toast = useToast();
  const [tab, setTab] = useState<OpsTab>("opsec");
  const [opsec, setOpsec] = useState<OpsecView | null>(null);
  const [rotating, setRotating] = useState(false);
  const [showRedacted, setShowRedacted] = useState(true);
  const [rawOps, setRawOps] = useState<Record<string, unknown>>({});

  const health = useAsyncResource<DeepHealth>(
    async (signal) =>
      normalizeDeepHealth(
        await apiCall(() => getHealth({ signal, timeout_ms: 10_000 }), "/api/health"),
      ),
    { pollMs: 20_000 },
  );
  const metrics = useAsyncResource<MetricsSnapshot>(
    (signal) =>
      apiCallOr(
        () => getMetrics({ signal, timeout_ms: 12_000 }),
        "/api/metrics",
        {} as MetricsSnapshot,
      ),
    { pollMs: 15_000 },
  );
  const config = useAsyncResource<Record<string, unknown>>(
    async (signal) => {
      const payload = await apiCall(
        () => apiFetch<Record<string, unknown>>("/api/config", { signal, timeout_ms: 15_000 }),
        "/api/config",
      );
      return payload && typeof payload === "object" ? payload : {};
    },
    { pollMs: 0 },
  );
  const opsecResource = useAsyncResource<unknown>(
    (signal) =>
      apiCall(
        () => apiFetch<unknown>("/api/opsec/status", { signal, timeout_ms: 10_000 }),
        "/api/opsec/status",
      ),
    { pollMs: 15_000, deps: [tab] },
  );

  const opsecView = useMemo<OpsecView | null>(() => {
    if (opsec) return opsec;
    const payload = opsecResource.data;
    if (!payload) return null;
    const rec = payload as Record<string, unknown>;
    const active = rec.active === true;
    const circuit = rec.circuit && typeof rec.circuit === "object" ? (rec.circuit as Record<string, unknown>) : null;
    return {
      active,
      mode: active ? "tor" : rec.circuit_id || circuit ? "unknown" : "direct",
      label: active ? "TOR" : rec.circuit_id || circuit ? "CIRCUIT" : "DIRECT",
      circuitId:
        typeof rec.circuit_id === "string"
          ? rec.circuit_id
          : typeof circuit?.id === "string"
            ? circuit.id
            : undefined,
      hops: typeof rec.hops === "number" ? rec.hops : typeof circuit?.hops === "number" ? circuit.hops : undefined,
      coverage: typeof rec.coverage === "number" ? rec.coverage : undefined,
      startedAt: typeof rec.started_at === "string" ? rec.started_at : undefined,
      expiresAt: typeof rec.expires_at === "string" ? rec.expires_at : undefined,
      detail: typeof rec.circuit_label === "string" ? rec.circuit_label : undefined,
    };
  }, [opsec, opsecResource.data]);

  const rotate = useCallback(async () => {
    setRotating(true);
    try {
      const next = await newCircuit({ timeout_ms: 30_000 });
      const view: OpsecView = {
        active: next.active === true,
        mode: next.active ? "tor" : "direct",
        label: next.active ? "TOR" : "DIRECT",
        circuitId: next.circuit_id ?? next.circuit?.id,
        hops: next.hops ?? next.circuit?.hops,
        coverage: next.coverage,
        startedAt: next.started_at ?? next.circuit?.created_at,
        expiresAt: next.expires_at ?? next.circuit?.expires_at,
      };
      setOpsec(view);
      opsecResource.reload();
      toast.success("New circuit established.");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setRotating(false);
    }
  }, [opsecResource, toast]);

  useMemo(() => {
    if (opsecResource.data && typeof opsecResource.data === "object") {
      setRawOps(opsecResource.data as Record<string, unknown>);
    }
  }, [opsecResource.data]);

  /* ── Connector matrix, derived from the health probes ── */
  const connectors: ConnectorRow[] = useMemo(() => {
    const checks = health.data?.checks ?? {};
    const names = new Set([
      "postgres",
      "redis",
      "neo4j",
      "qdrant",
      "minio",
      "llm",
      "opsec",
      ...Object.keys(checks),
    ]);
    return [...names]
      .map((name) => {
        const probe = checks[name];
        if (probe) {
          return { name, ok: probe.ok, detail: probe.detail, latency_ms: probe.latency_ms };
        }
        if (name === "llm" && health.data?.llm) {
          return {
            name,
            ok: health.data.llm.available !== false,
            detail: health.data.llm.model ?? health.data.llm.provider,
          };
        }
        if (name === "opsec") {
          return {
            name,
            ok: opsecView?.active ?? null,
            detail: opsecView?.circuitId,
          };
        }
        return { name, ok: null };
      })
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [health.data, opsecView]);

  const connectorColumns: Column<ConnectorRow>[] = useMemo(
    () => [
      {
        key: "name",
        header: "Connector",
        width: "170px",
        sortValue: (row) => row.name,
        render: (row) => <span className="font-mono tracking-wide text-ink">{row.name}</span>,
      },
      {
        key: "status",
        header: "Status",
        width: "110px",
        sortValue: (row) => (row.ok === null ? -1 : row.ok ? 1 : 0),
        render: (row) => (
          <StatusDot
            tone={row.ok === null ? "idle" : row.ok ? "ok" : "err"}
            dotOnly={false}
            label={`${row.name}: ${row.ok === null ? "not reported" : row.ok ? "healthy" : "down"}`}
          />
        ),
      },
      {
        key: "detail",
        header: "Detail",
        render: (row) => (
          <span className="block min-w-0 truncate text-ink-muted" title={row.detail}>
            {row.detail ?? (row.ok === null ? "not reported" : "—")}
          </span>
        ),
      },
      {
        key: "latency",
        header: "Latency",
        width: "92px",
        align: "right",
        hideBelow: "md",
        sortValue: (row) => row.latency_ms ?? -1,
        render: (row) =>
          typeof row.latency_ms === "number" ? formatDuration(row.latency_ms) : "—",
      },
    ],
    [],
  );

  const counters = useMemo(
    () => Object.entries(metrics.data?.counters ?? {}).sort(([a], [b]) => a.localeCompare(b)),
    [metrics.data],
  );
  const timings = useMemo(
    () => Object.entries(metrics.data?.timings ?? {}).sort(([a], [b]) => a.localeCompare(b)),
    [metrics.data],
  );
  const gauges = useMemo(
    () => Object.entries(metrics.data?.gauges ?? {}).sort(([a], [b]) => a.localeCompare(b)),
    [metrics.data],
  );

  const configRows = useMemo(
    () => Object.entries(config.data ?? {}).sort(([a], [b]) => a.localeCompare(b)),
    [config.data],
  );

  const healthTone: HealthLevel = health.data?.status ?? "degraded";

  return (
    <div className={clsx("scroll-thin min-h-0 flex-1 overflow-y-auto", className)}>
      <div className="flex flex-col gap-3 p-3">
        {/* Header stats */}
        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          <StatTile
            label="Backend"
            value={healthTone.toUpperCase()}
            tone={healthTone === "ok" ? "ok" : healthTone === "down" ? "err" : "warn"}
            hint={API_BASE ? API_BASE.replace(/^https?:\/\//, "") : "same-origin"}
            icon="⬢"
            loading={health.state === "loading" && !health.data}
          />
          <StatTile
            label="Version"
            value={health.data?.version ?? "—"}
            tone="idle"
            hint={`uptime ${health.data?.uptime_s !== undefined ? formatDuration(health.data.uptime_s * 1000) : "—"}`}
            icon="◈"
          />
          <StatTile
            label="Agents"
            value={`${health.data?.agents?.healthy ?? "—"}/${health.data?.agents?.total ?? "—"}`}
            tone="info"
            hint="healthy / registered"
            icon="⬡"
          />
          <StatTile
            label="Cache hit rate"
            value={
              health.data?.cache?.hit_rate === undefined
                ? "—"
                : `${Math.round(health.data.cache.hit_rate * 100)}%`
            }
            tone="warn"
            hint={health.data?.cache?.backend ?? "no cache reported"}
            icon="⚡"
            progress={health.data?.cache?.hit_rate}
          />
        </div>

        <Panel bodyClassName="!p-0">
          <Tabs
            label="Operations views"
            active={tab}
            onChange={setTab}
            items={[
              { id: "opsec", label: "OPSEC" },
              { id: "metrics", label: "Metrics", count: counters.length },
              { id: "config", label: "Config", count: configRows.length },
              { id: "connectors", label: "Connectors", count: connectors.length },
            ]}
            actions={
              <>
                {metrics.refreshing && <Badge tone="info" pulse>LIVE</Badge>}
                <Button
                  size="sm"
                  variant="ghost"
                  iconLabel="Refresh all telemetry"
                  onClick={() => {
                    health.reload();
                    metrics.reload();
                    opsecResource.reload();
                  }}
                >
                  ⟳
                </Button>
              </>
            }
          />

          <div className="p-2">
            {/* ── OPSEC ── */}
            {tab === "opsec" && (
              <div className="grid gap-2 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                <Panel eyebrow="CIRCUIT" title="Opsec status">
                  {opsecResource.state === "loading" && !opsecView ? (
                    <SkeletonRows rows={4} height={22} />
                  ) : opsecResource.state === "error" && !opsecView ? (
                    <ErrorState
                      compact
                      title="Opsec status unavailable"
                      message={opsecResource.error}
                      onRetry={opsecResource.reload}
                    />
                  ) : !opsecView ? (
                    <EmptyState
                      compact
                      glyph="◌"
                      title="NO OPSEC DATA"
                      description="The opsec endpoint returned no circuit state. Check that OPSEC_ENABLED is set on the backend, then retry."
                    />
                  ) : (
                    <div className="flex flex-col gap-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <OpsecIndicator
                          pollMs={0}
                          confirmRotate={false}
                          onRotated={(view) => setOpsec(view)}
                        />
                        <Button
                          variant="solid"
                          size="sm"
                          loading={rotating}
                          onClick={() => void rotate()}
                        >
                          Rotate circuit
                        </Button>
                      </div>

                      <div>
                        <DefRow label="Routed through circuit">
                          <Badge tone={opsecView.active ? "ok" : "err"}>
                            {opsecView.active ? "YES" : "NO"}
                          </Badge>
                        </DefRow>
                        {opsecView.circuitId && (
                          <DefRow label="Circuit" mono>
                            {opsecView.circuitId}
                          </DefRow>
                        )}
                        {opsecView.detail && <DefRow label="Label">{opsecView.detail}</DefRow>}
                        {opsecView.hops !== undefined && (
                          <DefRow label="Hops">{opsecView.hops}</DefRow>
                        )}
                        {opsecView.coverage !== undefined && (
                          <DefRow label="Coverage">
                            <Meter
                              value={opsecView.coverage}
                              tone={opsecView.coverage > 0.9 ? "ok" : "warn"}
                              className="ml-auto w-16"
                              label="Circuit coverage"
                            />
                            {Math.round(opsecView.coverage * 100)}%
                          </DefRow>
                        )}
                        {opsecView.startedAt && (
                          <DefRow label="Started">
                            {formatRelativeTime(opsecView.startedAt)}
                          </DefRow>
                        )}
                        {opsecView.expiresAt && (
                          <DefRow label="Expires">
                            {formatRelativeTime(opsecView.expiresAt)}
                          </DefRow>
                        )}
                      </div>

                      {opsecView.coverage !== undefined && opsecView.coverage < 1 && (
                        <p className="m-0 rounded border border-signal-warn/40 bg-signal-warn/10 p-2 text-[10px] leading-relaxed text-signal-warn">
                          Only {Math.round(opsecView.coverage * 100)}% of traffic is inside the
                          circuit. Requests outside it would expose this operator's egress address —
                          rotate before running anything sensitive.
                        </p>
                      )}

                      {opsecResource.state === "error" && (
                        <p role="alert" className="m-0 text-[10px] text-signal-err">
                          {opsecResource.error}
                        </p>
                      )}
                    </div>
                  )}
                </Panel>

                <Panel eyebrow="RAW" title="Opsec payload" bodyClassName="!p-0">
                  {Object.keys(rawOps).length === 0 ? (
                    <EmptyState
                      compact
                      glyph="◌"
                      title="NO PAYLOAD"
                      description="The raw inspector mirrors /api/opsec/status exactly; an empty object means the endpoint answered with no fields."
                    />
                  ) : (
                    <pre className="scroll-thin m-0 max-h-[320px] overflow-auto p-2 text-[9px] leading-relaxed text-ink-muted">
                      {JSON.stringify(
                        Object.fromEntries(
                          Object.entries(rawOps).map(([key, value]) => [
                            key,
                            showRedacted ? redactConfigValue(key, value) : value,
                          ]),
                        ),
                        null,
                        2,
                      )}
                    </pre>
                  )}
                </Panel>
              </div>
            )}

            {/* ── Metrics ── */}
            {tab === "metrics" && (
              <div className="flex flex-col gap-2">
                {metrics.state === "loading" && !metrics.data ? (
                  <SkeletonGrid count={6} minWidth={200} height={70} />
                ) : metrics.state === "error" ? (
                  <ErrorState message={metrics.error} onRetry={metrics.reload} />
                ) : (
                  <>
                    <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                      <StatTile
                        label="Uptime"
                        value={
                          metrics.data?.uptime_s === undefined
                            ? "—"
                            : formatDuration(metrics.data.uptime_s * 1000)
                        }
                        tone="idle"
                        icon="⏱"
                      />
                      <StatTile
                        label="Counters"
                        value={counters.length}
                        tone="info"
                        icon="∑"
                        hint="distinct metric names"
                      />
                      <StatTile
                        label="Timers"
                        value={timings.length}
                        tone="ok"
                        icon="⏲"
                        hint="timed operations"
                      />
                      <StatTile
                        label="Gauges"
                        value={gauges.length}
                        tone="warn"
                        icon="◈"
                        hint="instantaneous values"
                      />
                    </div>

                    <div className="grid gap-2 lg:grid-cols-3">
                      <Panel eyebrow="COUNTERS" title="Counters" bodyClassName="!p-0">
                        {counters.length === 0 ? (
                          <EmptyState
                            compact
                            glyph="◌"
                            title="NO COUNTERS"
                            description="Monotonic counters appear once the backend has served traffic. Reset the process to zero them."
                          />
                        ) : (
                          <div className="scroll-thin max-h-[280px] overflow-y-auto">
                            <ul className="m-0 flex list-none flex-col">
                              {counters.map(([key, value]) => (
                                <li
                                  key={key}
                                  className="flex items-center justify-between gap-2 border-b border-border-subtle/60 px-2 py-1"
                                >
                                  <span className="min-w-0 truncate text-[10px] text-ink-muted">
                                    {key}
                                  </span>
                                  <span className="shrink-0 text-[11px] tabular-nums text-accent">
                                    {formatCount(value)}
                                  </span>
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </Panel>

                      <Panel eyebrow="TIMINGS" title="Operations" bodyClassName="!p-0">
                        {timings.length === 0 ? (
                          <EmptyState
                            compact
                            glyph="◌"
                            title="NO TIMINGS"
                            description="Per-operation timings are recorded on first call. Run an investigation and this fills in."
                          />
                        ) : (
                          <div className="scroll-thin max-h-[280px] overflow-y-auto">
                            <ul className="m-0 flex list-none flex-col">
                              {timings.map(([key, timing]) => (
                                <li
                                  key={key}
                                  className="flex flex-col gap-0.5 border-b border-border-subtle/60 px-2 py-1"
                                >
                                  <span className="flex items-center justify-between gap-2">
                                    <span className="min-w-0 truncate text-[10px] text-ink-muted">
                                      {key}
                                    </span>
                                    <span className="shrink-0 text-[10px] tabular-nums text-accent">
                                      {formatDuration((timing.avg_s ?? 0) * 1000)}
                                    </span>
                                  </span>
                                  <span className="mono-label !text-[8px]">
                                    {timing.count ?? 0} calls · max{" "}
                                    {timing.max_s === undefined ? "—" : formatDuration(timing.max_s * 1000)}
                                  </span>
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </Panel>

                      <Panel eyebrow="GAUGES" title="Instantaneous" bodyClassName="!p-0">
                        {gauges.length === 0 ? (
                          <EmptyState
                            compact
                            glyph="◌"
                            title="NO GAUGES"
                            description="Gauges are instantaneous values such as active agents. They register once the pipeline runs."
                          />
                        ) : (
                          <div className="scroll-thin max-h-[280px] overflow-y-auto">
                            <ul className="m-0 flex list-none flex-col">
                              {gauges.map(([key, value]) => (
                                <li
                                  key={key}
                                  className="flex items-center justify-between gap-2 border-b border-border-subtle/60 px-2 py-1"
                                >
                                  <span className="min-w-0 truncate text-[10px] text-ink-muted">
                                    {key}
                                  </span>
                                  <span className="shrink-0 text-[11px] tabular-nums text-accent">
                                    {formatCount(value)}
                                  </span>
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </Panel>
                    </div>
                  </>
                )}
              </div>
            )}

            {/* ── Config ── */}
            {tab === "config" && (
              <div className="flex flex-col gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="warn">SECRETS REDACTED</Badge>
                  <p className="m-0 text-[10px] text-ink-muted">
                    Keys matching pass / secret / token / key / auth are masked before rendering.
                  </p>
                  <span className="flex-1" />
                  <Button size="sm" variant="ghost" onClick={config.reload}>
                    Refresh
                  </Button>
                </div>

                {config.state === "loading" && !config.data ? (
                  <SkeletonRows rows={6} height={26} />
                ) : config.state === "error" ? (
                  <ErrorState
                    title="Config endpoint unavailable"
                    message={`${config.error} — this backend does not expose GET /api/config.`}
                    onRetry={config.reload}
                  />
                ) : configRows.length === 0 ? (
                  <EmptyState
                    glyph="⚙"
                    title="NO CONFIG REPORTED"
                    description="The backend returned an empty configuration object."
                  />
                ) : (
                  <Panel bodyClassName="!p-0">
                    <Table
                      columns={[
                        {
                          key: "key",
                          header: "Setting",
                          width: "260px",
                          sortValue: (row: [string, unknown]) => row[0],
                          render: (row: [string, unknown]) => (
                            <span className="font-mono text-[11px] text-ink">{row[0]}</span>
                          ),
                        },
                        {
                          key: "value",
                          header: "Value",
                          render: (row: [string, unknown]) => (
                            <span className="block min-w-0 truncate text-ink-muted">
                              {showRedacted
                                ? redactConfigValue(row[0], row[1])
                                : String(row[1])}
                            </span>
                          ),
                        },
                      ]}
                      rows={configRows}
                      rowKey={(row) => row[0]}
                      caption="Runtime configuration"
                    />
                  </Panel>
                )}
              </div>
            )}

            {/* ── Connectors ── */}
            {tab === "connectors" && (
              <Panel
                eyebrow="DEPENDENCIES"
                title="Connector health matrix"
                actions={
                  <>
                    <Badge tone="ok">
                      {connectors.filter((c) => c.ok === true).length} up
                    </Badge>
                    <Badge tone="err">
                      {connectors.filter((c) => c.ok === false).length} down
                    </Badge>
                    <Badge tone="idle">
                      {connectors.filter((c) => c.ok === null).length} unknown
                    </Badge>
                  </>
                }
                bodyClassName="!p-0"
              >
                {health.state === "loading" && !health.data ? (
                  <div className="p-2">
                    <SkeletonRows rows={6} height={28} />
                  </div>
                ) : health.state === "error" ? (
                  <ErrorState message={health.error} onRetry={health.reload} />
                ) : connectors.length === 0 ? (
                  <EmptyState
                      glyph="⚡"
                      title="NO CONNECTORS REPORTED"
                      description="Connector availability comes from /api/agents. This backend either omits the field or has no connectors configured — individual agents still run, just without live enrichment."
                    />
                ) : (
                  <Table
                    columns={connectorColumns}
                    rows={connectors}
                    rowKey={(row) => row.name}
                    caption="Connector health"
                  />
                )}
              </Panel>
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}

/** Compact number formatting for metric values. */
function formatCount(value: number | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (Math.abs(value) >= 10_000) return `${(value / 1000).toFixed(1)}k`;
  return value.toLocaleString();
}
