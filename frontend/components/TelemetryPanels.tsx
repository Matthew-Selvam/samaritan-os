"use client";

/**
 * TelemetryPanels.tsx — the dashboard's computed telemetry, built on
 * `lib/heatmap.ts`.
 *
 * `lib/heatmap.ts` shipped a complete, pure geometry engine (activity grid,
 * confidence histogram, sparkline, stacked bars, colour scales) that nothing
 * consumed. These panels are where it earns its place: every shape below is
 * computed from the real run payload — `/api/metrics` timings/counters, the
 * current investigation's entities, signals and timeline — never from fixtures.
 *
 * Three panels, in the order an analyst reads them:
 *   1. **Signal activity** — a 7×24 day/hour grid over every dated signal.
 *      Answers "when do signals arrive?" and makes collection windows obvious.
 *   2. **Confidence distribution** — a histogram over entity + signal
 *      confidence, with mean/median called out. Answers "how much of this run
 *      should I trust?".
 *   3. **Pipeline latency** — one sparkline per timed operation from
 *      `/api/metrics`, so a regression in `investigate` is visible at a glance
 *      rather than buried in a JSON dump on the OPS view.
 *
 * Every panel is present in all three states. With no data the panel explains
 * what would fill it instead of rendering an empty frame.
 *
 * Accessibility: the heatmap is an `role="img"` with a text summary plus a
 * visually-hidden data table, the histogram exposes each bin through
 * `role="meter"`, and the sparkline carries its own `aria-label`. Nothing here
 * relies on colour alone — each value is also printed.
 */

import { useMemo } from "react";
import clsx from "clsx";
import { Badge, EmptyState, Panel, SkeletonRows, StatTile } from "@/components/ui";
import {
  buildActivityHeatmap,
  confidenceHistogram,
  sparklinePath,
  type ConfidenceHistogram,
  type SignalActivityHeatmap,
  type SparklinePath,
} from "@/lib/heatmap";
import type {
  Entity,
  Investigation,
  MetricsSnapshot,
  Signal,
  TimelineEvent,
} from "@/lib/types";
import { formatDuration, truncate } from "@/lib/format";

/** Props for {@link TelemetryPanels}. */
export interface TelemetryPanelsProps {
  /** Current run — supplies entities, signals and timeline. */
  readonly investigation?: Investigation | null;
  /** Timeline events for the current run; folded into the activity grid. */
  readonly timeline?: readonly TimelineEvent[];
  /** `/api/metrics` snapshot; supplies latency sparklines. */
  readonly metrics?: MetricsSnapshot | null;
  /** True while either source is still in flight. */
  readonly loading?: boolean;
  /** Extra classes on the wrapping grid. */
  readonly className?: string;
}

/**
 * Collect every timestamp the run carries, newest data included.
 *
 * Signals with a `ts` are the primary source; entity `first_seen`/`last_seen`
 * and timeline events are folded in so a run that produced mostly
 * relationships rather than dated signals still renders a grid.
 */
function collectTimestamps(
  entities: readonly Entity[],
  signals: readonly Signal[],
  timeline: readonly TimelineEvent[],
): readonly (string | null)[] {
  const out: (string | null)[] = [];
  for (const signal of signals) out.push(signal.ts ?? null);
  for (const entity of entities) {
    out.push(entity.first_seen ?? null);
    out.push(entity.last_seen ?? null);
  }
  for (const event of timeline) out.push(event.ts ?? null);
  return out;
}

/** A single timed operation from `/api/metrics`, flattened for the sparkline. */
interface LatencySeries {
  readonly key: string;
  readonly path: SparklinePath;
  readonly avgMs: number | null;
  readonly calls: number;
}

/**
 * Turn the metrics `timings` map into one sparkline per operation.
 *
 * The backend reports cumulative `count`/`total_s` per timer rather than a
 * windowed series, so there is nothing to plot a curve from — a single point
 * is the honest input. Rather than invent a curve, each sparkline plots that
 * one real point as a flat baseline and the number is printed beside it. If a
 * future backend sends an `avg_s` series we plot it properly.
 */
function buildLatencySeries(metrics: MetricsSnapshot | null | undefined): LatencySeries[] {
  const timings = metrics?.timings ?? {};
  return Object.entries(timings)
    .map(([key, timing]) => {
      const count = typeof timing.count === "number" ? timing.count : 0;
      const avgS =
        typeof timing.avg_s === "number"
          ? timing.avg_s
          : count > 0 && typeof timing.total_s === "number"
            ? timing.total_s / count
            : null;
      // One real point per sample we can prove. `sparklinePath` is total, so
      // an empty/NaN input yields empty geometry rather than throwing.
      const points: number[] = [];
      if (typeof timing.max_s === "number" && count > 0) {
        points.push(avgS ?? 0, timing.max_s);
      } else if (avgS !== null) {
        points.push(avgS);
      }
      return {
        key,
        path: sparklinePath(points, { width: 100, height: 22, padding: 2 }),
        avgMs: avgS === null ? null : avgS * 1000,
        calls: count,
      };
    })
    .sort((a, b) => (b.avgMs ?? -1) - (a.avgMs ?? -1));
}

/* ── Panel 1 — signal activity ─────────────────────────────────────────────── */

function ActivityPanel({ heatmap }: { heatmap: SignalActivityHeatmap }) {
  const summary =
    heatmap.total === 0
      ? "No dated signals in this run yet."
      : `${heatmap.total} dated observations across 7 days. Peak: ${
          heatmap.peak ? `${heatmap.days[heatmap.peak.day]} ${String(heatmap.peak.hour).padStart(2, "0")}:00` : "—"
        } (${heatmap.peak?.count ?? 0}).`;

  return (
    <Panel
      eyebrow="ACTIVITY"
      title="Signal activity"
      actions={
        <Badge tone={heatmap.total > 0 ? "ok" : "idle"}>{heatmap.total} DATED</Badge>
      }
    >
      {heatmap.total === 0 ? (
        <EmptyState
          compact
          glyph="▦"
          title="NO DATED SIGNALS"
          description="This grid fills from every timestamped signal, entity sighting and timeline event in the run. Signals that carry no date are counted in the confidence panel instead."
        />
      ) : (
        <div className="flex flex-col gap-2">
          <div
            role="img"
            aria-label={`Signal activity by day and hour. ${summary}`}
            className="flex gap-[2px]"
          >
            {heatmap.days.map((day, dayIndex) => (
              <div key={day} className="flex flex-col items-center gap-[2px]">
                <div
                  className="flex flex-col gap-[2px]"
                  title={`${day}: ${heatmap.cells
                    .filter((cell) => cell.day === dayIndex)
                    .reduce((sum, cell) => sum + cell.count, 0)} signal(s)`}
                >
                  {heatmap.hours.map((hour) => {
                    const cell = heatmap.cells[dayIndex * 24 + hour];
                    return (
                      <span
                        key={hour}
                        className="h-[5px] w-[5px] rounded-[1px]"
                        style={{ background: cell.color, opacity: cell.count > 0 ? 1 : 0.18 }}
                      />
                    );
                  })}
                </div>
                <span className="mono-label !text-[7px] leading-none">{day.slice(0, 1)}</span>
              </div>
            ))}
          </div>
          <p className="m-0 text-[9px] leading-relaxed text-ink-muted">{summary}</p>
          {/* Screen readers get the numbers; sighted users get the shape. */}
          <table className="sr-only">
            <caption>Signal counts by weekday</caption>
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col">Signals</th>
              </tr>
            </thead>
            <tbody>
              {heatmap.days.map((day, dayIndex) => (
                <tr key={day}>
                  <th scope="row">{day}</th>
                  <td>
                    {heatmap.cells
                      .filter((cell) => cell.day === dayIndex)
                      .reduce((sum, cell) => sum + cell.count, 0)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

/* ── Panel 2 — confidence distribution ─────────────────────────────────────── */

function ConfidencePanel({ histogram }: { histogram: ConfidenceHistogram }) {
  return (
    <Panel
      eyebrow="RELIABILITY"
      title="Confidence distribution"
      actions={
        <Badge tone={histogram.median !== null && histogram.median >= 0.7 ? "ok" : "warn"}>
          {histogram.total} SCORED
        </Badge>
      }
    >
      {histogram.total === 0 ? (
        <EmptyState
          compact
          glyph="◑"
          title="NOTHING SCORED YET"
          description="Every entity and signal carries a 0–1 confidence. Run an investigation and this becomes a histogram you can use to decide which claims to verify first."
        />
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-end gap-[2px]" role="group" aria-label="Confidence histogram">
            {histogram.bins.map((bin) => (
              <span
                key={`${bin.from}-${bin.to}`}
                role="meter"
                aria-valuenow={bin.count}
                aria-valuemin={0}
                aria-valuemax={Math.max(1, histogram.maxCount)}
                aria-label={`Confidence ${bin.label}: ${bin.count} item(s)`}
                title={`${bin.label} — ${bin.count}`}
                className="flex-1 rounded-t-[2px]"
                style={{
                  height: `${Math.max(4, (bin.ratio || 0) * 56)}px`,
                  background: bin.color,
                  opacity: bin.count > 0 ? 1 : 0.15,
                }}
              />
            ))}
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="mono-label !text-[8px]">0.0</span>
            <span className="mono-label !text-[8px]">1.0 CONFIDENCE</span>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <StatTile label="Mean" value={histogram.mean === null ? "—" : `${Math.round(histogram.mean * 100)}%`} tone="info" />
            <StatTile
              label="Median"
              value={histogram.median === null ? "—" : `${Math.round(histogram.median * 100)}%`}
              tone={histogram.median !== null && histogram.median >= 0.7 ? "ok" : "warn"}
            />
            <StatTile
              label="High conf"
              value={histogram.bins
                .filter((bin) => bin.from >= 0.7)
                .reduce((sum, bin) => sum + bin.count, 0)}
              tone="ok"
              hint="≥ 0.70"
            />
          </div>
        </div>
      )}
    </Panel>
  );
}

/* ── Panel 3 — pipeline latency ────────────────────────────────────────────── */

function LatencyPanel({ series }: { series: readonly LatencySeries[] }) {
  return (
    <Panel
      eyebrow="PERFORMANCE"
      title="Pipeline latency"
      actions={<Badge tone={series.length > 0 ? "info" : "idle"}>{series.length} TIMERS</Badge>}
    >
      {series.length === 0 ? (
        <EmptyState
          compact
          glyph="⏱"
          title="NO TIMERS REPORTED"
          description="The backend exposes per-operation latency at /api/metrics/timings. Once any operation has been timed, each one appears here with its mean and worst case."
        />
      ) : (
        <ul className="m-0 flex list-none flex-col gap-2">
          {series.slice(0, 6).map((row) => (
            <li key={row.key} className="flex flex-col gap-0.5">
              <div className="flex items-center justify-between gap-2">
                <span className="min-w-0 truncate text-[10px] text-ink-muted">
                  {truncate(row.key, 26)}
                </span>
                <span className="shrink-0 text-[10px] tabular-nums text-accent">
                  {row.avgMs === null ? "—" : formatDuration(row.avgMs)}
                </span>
              </div>
              <svg
                viewBox="0 0 100 22"
                preserveAspectRatio="none"
                className="h-[22px] w-full"
                role="img"
                aria-label={`${row.key}: mean ${
                  row.avgMs === null ? "unknown" : formatDuration(row.avgMs)
                } over ${row.calls} call(s)`}
              >
                {row.path.area ? <path d={row.path.area} fill="var(--tint-accent-10)" /> : null}
                <path d={row.path.d} fill="none" stroke="var(--green)" strokeWidth="1.2" />
              </svg>
              <span className="mono-label !text-[8px]">{row.calls} calls</span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

/**
 * The telemetry row: activity, confidence and latency.
 *
 * @example
 * <TelemetryPanels investigation={inv} metrics={metrics.data} loading={metrics.state === "loading"} />
 */
export function TelemetryPanels({
  investigation = null,
  timeline = [],
  metrics = null,
  loading = false,
  className,
}: TelemetryPanelsProps) {
  const entities = investigation?.report?.entities ?? [];
  const signals = investigation?.report?.signals ?? [];

  const heatmap = useMemo(
    () => buildActivityHeatmap(collectTimestamps(entities, signals, timeline)),
    [entities, signals, timeline],
  );

  const histogram = useMemo(
    () =>
      confidenceHistogram(
        [
          ...entities.map((entity) => entity.confidence),
          ...signals.map((signal) => signal.confidence),
        ],
        { bins: 10 },
      ),
    [entities, signals],
  );

  const series = useMemo(() => buildLatencySeries(metrics), [metrics]);

  if (loading) {
    return (
      <div className={clsx("grid gap-3 xl:grid-cols-3", className)}>
        <TelemetryPanelsSkeleton />
      </div>
    );
  }

  return (
    <div className={clsx("grid gap-3 xl:grid-cols-3", className)}>
      <ActivityPanel heatmap={heatmap} />
      <ConfidencePanel histogram={histogram} />
      <LatencyPanel series={series} />
    </div>
  );
}

/** The three panel slots, shown while the first telemetry payload is in flight. */
function TelemetryPanelsSkeleton() {
  return (
    <>
      {[0, 1, 2].map((index) => (
        <div key={index} className="panel">
          <SkeletonRows rows={3} height={26} />
        </div>
      ))}
    </>
  );
}
