"use client";

/**
 * DashboardView.tsx — the mission screen.
 *
 * Answers "is the platform healthy and what has it found lately" in one screen:
 * six headline stats from `/api/metrics`, the service-health grid from
 * `/api/health/deep`, the signal/entity tallies for the current run, recent
 * investigations, and the input bar front and centre.
 *
 * Every panel has all three states — skeleton while loading, error with retry on
 * failure, explicit empty state when the data is legitimately empty.
 */

import { useCallback, useMemo } from "react";
import clsx from "clsx";
import {
  Badge,
  Button,
  ColorDot,
  EmptyState,
  ErrorState,
  Meter,
  Panel,
  SkeletonGrid,
  SkeletonRows,
  StatTile,
  Toggle,
} from "@/components/ui";
import { InputBar } from "@/components/InputBar";
import { InputBarHandle } from "@/components/InputBar";
import { StatusDot } from "@/components/StatusDot";
import { CasePanel } from "@/components/CasePanel";
import { getHealth, getMetrics } from "@/lib/api";
import {
  colorFor,
  labelFor,
} from "@/lib/entityTypes";
import {
  formatDuration,
  statusLabel,
  statusTone,
  truncate,
} from "@/lib/format";
import type { HealthLevel, Investigation, MetricsSnapshot } from "@/lib/types";
import {
  deriveStats,
  normalizeDeepHealth,
  probeFor,
  SERVICE_KEYS,
  SERVICE_META,
  tallyByEntityType,
  tallyBySource,
  type DeepHealth,
  type ServiceKey,
  type TallyRow,
} from "@/lib/views";
import { useAsyncResource } from "@/lib/hooks";

export interface DashboardViewProps {
  /** Bar ref so `/` and the palette can focus the field from here too. */
  inputRef?: React.Ref<InputBarHandle>;
  onSubmit: (value: string) => void;
  running: boolean;
  caseLabel?: string | null;
  /** Current run, used for the entity/signal tallies. */
  investigation?: Investigation | null;
  /** Refreshed on every change so the tallies follow a live run. */
  investigationNonce?: number;
  onOpenCase?: (investigation: Investigation) => void;
  onOpenOps?: () => void;
  onNavigate?: (view: string) => void;
  className?: string;
}

/** Health payload source: `/api/health/deep` first, `/api/health` as fallback. */
async function loadHealth(signal: AbortSignal): Promise<DeepHealth> {
  try {
    return normalizeDeepHealth(
      await getHealth({ signal, timeout_ms: 8_000 }),
    );
  } catch (err) {
    // A backend without the deep route still answers /api/health.
    if (err instanceof Error && /\b404\b/.test(err.message)) {
      return normalizeDeepHealth(
        await getHealth({ signal, timeout_ms: 8_000 }),
      );
    }
    throw err;
  }
}

/** Metrics source with the same graceful degradation. */
async function loadMetrics(signal: AbortSignal): Promise<MetricsSnapshot> {
  return getMetrics({ signal, timeout_ms: 12_000 });
}

/** Health level for one probe. */
function probeTone(ok: boolean | null): "ok" | "warn" | "err" | "idle" {
  if (ok === null) return "idle";
  return ok ? "ok" : "err";
}

/**
 * Mission screen.
 *
 * @example
 * <DashboardView onSubmit={run} running={busy} investigation={inv} />
 */
export function DashboardView({
  inputRef,
  onSubmit,
  running,
  caseLabel = null,
  investigation = null,
  investigationNonce = 0,
  onOpenCase,
  onOpenOps,
  onNavigate,
  className,
}: DashboardViewProps) {
  const health = useAsyncResource<DeepHealth>(loadHealth, {
    pollMs: 20_000,
    deps: [],
  });
  const metrics = useAsyncResource<MetricsSnapshot>(loadMetrics, {
    pollMs: 15_000,
    deps: [],
  });

  const refreshAll = useCallback(() => {
    health.reload();
    metrics.reload();
  }, [health, metrics]);

  const stats = useMemo(
    () =>
      deriveStats({
        counters: metrics.data?.counters,
        timings: metrics.data?.timings,
        gauges: metrics.data?.gauges,
        activeAgents:
          investigation?.agents?.filter((a) => a.status === "done").length ?? 0,
        entities: investigation?.report?.entities?.length ?? investigation?.entities?.nodes?.length,
        signals: investigation?.report?.signals?.length,
      }),
    [metrics.data, investigation],
  );

  const entities = investigation?.report?.entities ?? [];
  const signals = investigation?.report?.signals ?? [];

  const entityTally = useMemo(
    () => tallyByEntityType(entities, colorFor),
    [entities],
  );
  const sourceTally = useMemo(() => tallyBySource(signals), [signals]);

  const healthLevel: HealthLevel | null = health.data?.status ?? null;
  const okServices = SERVICE_KEYS.filter((key) => probeFor(health.data, key)?.ok === true).length;
  const reportedServices = SERVICE_KEYS.filter((key) => probeFor(health.data, key) !== null).length;

  return (
    <div className={clsx("scroll-thin min-h-0 flex-1 overflow-y-auto", className)}>
      <div className="flex flex-col gap-3 p-3">
        {/* ── Input: front and centre ── */}
        <Panel
          eyebrow="TARGET"
          title="New investigation"
          actions={
            running ? (
              <Badge tone="info" pulse>
                RUNNING
              </Badge>
            ) : (
              <Badge tone="idle">READY</Badge>
            )
          }
        >
          <InputBar ref={inputRef} onSubmit={onSubmit} running={running} caseLabel={caseLabel} />
        </Panel>

        {/* ── Headline stats ── */}
        <section aria-label="Platform statistics">
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
            <StatTile
              label="Investigations"
              value={stats.investigationsRun.toLocaleString()}
              icon="◎"
              tone="info"
              loading={metrics.state === "loading" && !metrics.data}
              hint="Total runs this process"
              title="Investigations run since the backend started"
            />
            <StatTile
              label="Agents active"
              value={investigation?.agents_activated?.length ?? stats.agentsActive}
              icon="⬡"
              tone="ok"
              hint={
                investigation?.agents_activated?.length
                  ? `Running for ${truncate(investigation.input, 24)}`
                  : "No run in flight"
              }
              title="Agents activated in the current run"
            />
            <StatTile
              label="Entities"
              value={stats.entitiesDiscovered.toLocaleString()}
              icon="◉"
              tone="info"
              loading={metrics.state === "loading" && !metrics.data}
              hint={entityTally.length > 0 ? `${entityTally.length} distinct types` : "No entities yet"}
            />
            <StatTile
              label="Signals"
              value={stats.signalsCollected.toLocaleString()}
              icon="≋"
              tone="warn"
              loading={metrics.state === "loading" && !metrics.data}
              hint={sourceTally.length > 0 ? `${sourceTally.length} sources` : "No signals yet"}
            />
            <StatTile
              label="Avg latency"
              value={stats.avgLatencyMs === null ? "—" : formatDuration(stats.avgLatencyMs)}
              icon="⏱"
              tone={stats.avgLatencyMs !== null && stats.avgLatencyMs > 30_000 ? "warn" : "ok"}
              loading={metrics.state === "loading" && !metrics.data}
              hint={stats.avgLatencyMs === null ? "No timing data reported" : "Mean pipeline duration"}
            />
            <StatTile
              label="Error rate"
              value={stats.errorRatePct === null ? "—" : `${stats.errorRatePct.toFixed(1)}%`}
              icon="⚠"
              tone={
                stats.errorRatePct === null ? "idle" : stats.errorRatePct > 5 ? "err" : "ok"
              }
              progress={stats.errorRatePct === null ? undefined : Math.min(1, stats.errorRatePct / 20)}
              hint={stats.errorRatePct === null ? "No error counters" : "errors / requests"}
            />
          </div>
        </section>

        {/* ── Service health ── */}
        <Panel
          eyebrow="TELEMETRY"
          title="Service health"
          actions={
            <>
              {health.refreshing && <Badge tone="info" pulse>LIVE</Badge>}
              <Badge tone={health.state === "error" ? "err" : okServices === SERVICE_KEYS.length ? "ok" : "warn"}>
                {health.state === "error" ? "UNREACHABLE" : `${okServices}/${SERVICE_KEYS.length} OK`}
              </Badge>
              <Button size="sm" variant="ghost" iconLabel="Refresh health" onClick={refreshAll}>
                ⟳
              </Button>
            </>
          }
        >
          {health.state === "loading" && !health.data ? (
            <SkeletonGrid count={7} minWidth={150} height={52} />
          ) : health.state === "error" ? (
            <ErrorState
              compact
              title="Backend unreachable"
              message={health.error}
              onRetry={health.reload}
            />
          ) : (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-7">
              {SERVICE_KEYS.map((key) => (
                <ServiceTile key={key} service={key} health={health.data} />
              ))}
            </div>
          )}
          {reportedServices < SERVICE_KEYS.length && health.data && (
            <p className="mt-2 text-[9px] text-ink-muted">
              {SERVICE_KEYS.length - reportedServices} dependenc
              {SERVICE_KEYS.length - reportedServices === 1 ? "y is" : "ies are"} not reported by
              this backend — shown as unknown rather than healthy.
            </p>
          )}
        </Panel>

        <div className="grid gap-3 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
          {/* ── Tallies ── */}
          <div className="flex flex-col gap-3">
            <TallyPanel
              title="Entities by type"
              eyebrow="GRAPH"
              rows={entityTally}
              loading={investigation === null && metrics.state === "loading"}
              emptyTitle="NO ENTITY BREAKDOWN"
              emptyDescription={
                investigation
                  ? "This run returned no entities."
                  : "Run an investigation to see the entity-type breakdown."
              }
              total={entities.length}
            />
            <TallyPanel
              title="Signals by source"
              eyebrow="SIGNALS"
              rows={sourceTally}
              loading={investigation === null && metrics.state === "loading"}
              emptyTitle="NO SIGNAL BREAKDOWN"
              emptyDescription={
                investigation
                  ? "This run returned no signals."
                  : "Run an investigation to see which connectors contributed."
              }
              total={signals.length}
            />
          </div>

          {/* ── Recent work ── */}
          <div className="flex min-h-0 flex-col gap-3">
            {investigation && (
              <Panel
                eyebrow="CURRENT RUN"
                title="Active investigation"
                actions={
                  <Button size="sm" variant="ghost" onClick={() => onNavigate?.("investigate")}>
                    Open ↗
                  </Button>
                }
              >
                <div className="flex flex-col gap-2">
                  <p className="m-0 break-all text-[12px] text-ink">{investigation.input}</p>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Badge tone={statusTone(investigation.status)} pulse={investigation.status === "running"}>
                      {statusLabel(investigation.status)}
                    </Badge>
                    <Badge tone="idle">{investigation.input_type || "unknown"}</Badge>
                    {(investigation.agents_activated?.length ?? 0) > 0 && (
                      <Badge tone="info">
                        {(investigation.agents_activated ?? []).length} agents
                      </Badge>
                    )}
                    {investigation.report?.confidence !== undefined && (
                      <Badge tone="ok">
                        {Math.round(investigation.report.confidence * 100)}% confidence
                      </Badge>
                    )}
                  </div>
                  {investigation.report?.summary && (
                    <p className="m-0 text-[10px] leading-relaxed text-ink-muted">
                      {investigation.report.summary}
                    </p>
                  )}
                </div>
              </Panel>
            )}

            <div className="min-h-[280px] flex-1">
              <CasePanel
                onSelect={(inv) => onOpenCase?.(inv)}
                activeId={investigation?.inv_id ?? null}
                caseId={null}
                className="h-full"
                pollMs={20_000}
              />
            </div>

            {onOpenOps && (
              <Button variant="outline" size="sm" onClick={onOpenOps} full>
                Open OPS console ↗
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── Service tile ───────────────────────────────────────────────────────────── */

function ServiceTile({ service, health }: { service: ServiceKey; health: DeepHealth | null }) {
  const probe = probeFor(health, service);
  const meta = SERVICE_META[service];
  const tone = probeTone(probe?.ok ?? null);
  return (
    <div
      className="panel flex flex-col gap-1 px-2 py-1.5"
      title={probe?.detail ? `${meta.hint}: ${probe.detail}` : `${meta.hint}: not reported`}
    >
      <div className="flex items-center justify-between gap-1">
        <span className="mono-label truncate !text-[9px]">{meta.label}</span>
        <StatusDot tone={tone} dotOnly label={`${meta.label}: ${probe ? (probe.ok ? "healthy" : "down") : "unknown"}`} />
      </div>
      <span className="truncate text-[9px] text-ink-muted">
        {probe ? (probe.detail ?? (probe.ok ? "healthy" : "unreachable")) : "not reported"}
      </span>
      {typeof probe?.latency_ms === "number" && (
        <Meter
          value={Math.max(0, Math.min(1, 1 - probe.latency_ms / 2000))}
          tone={tone}
          height={2}
          label={`${meta.label} latency`}
        />
      )}
    </div>
  );
}

/* ── Tally panel ────────────────────────────────────────────────────────────── */

function TallyPanel({
  title,
  eyebrow,
  rows,
  loading,
  emptyTitle,
  emptyDescription,
  total,
}: {
  title: string;
  eyebrow: string;
  rows: readonly TallyRow[];
  loading: boolean;
  emptyTitle: string;
  emptyDescription: string;
  total: number;
}) {
  return (
    <Panel title={title} eyebrow={eyebrow} actions={<Badge tone="idle">{total}</Badge>}>
      {loading ? (
        <SkeletonRows rows={4} height={22} />
      ) : rows.length === 0 ? (
        <EmptyState compact glyph="◌" title={emptyTitle} description={emptyDescription} />
      ) : (
        <ul className="flex flex-col gap-1.5">
          {rows.slice(0, 12).map((row) => (
            <li key={row.key} className="flex items-center gap-2">
              <ColorDot color={row.color} />
              <span className="w-[110px] shrink-0 truncate text-[10px] text-ink">
                {eyebrow === "GRAPH" ? labelFor(row.key) : row.label}
              </span>
              <Meter
                value={row.share / 100}
                tone={row.share > 60 ? "ok" : row.share > 25 ? "info" : "idle"}
                height={5}
                label={`${row.label} share`}
              />
              <span className="w-9 shrink-0 text-right text-[10px] tabular-nums text-ink-muted">
                {row.count}
              </span>
              <span className="w-11 shrink-0 text-right text-[9px] tabular-nums text-ink-faint">
                {row.share.toFixed(0)}%
              </span>
            </li>
          ))}
          {rows.length > 12 && (
            <li className="mono-label pt-0.5 !text-[8px]">
              +{rows.length - 12} more
            </li>
          )}
        </ul>
      )}
    </Panel>
  );
}
