"use client";

/**
 * TimelineView.tsx — KRONOS chronology on a vis.js timeline.
 *
 * vis is loaded client-side only (it touches `window` at import time) and its
 * dark theme is applied programmatically so the axis matches the rest of the
 * console without pulling a CSS file into the bundle. Filters run before the
 * dataset is pushed, so a filtered timeline redraws instantly.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import {
  Badge,
  Button,
  DefRow,
  Drawer,
  EmptyState,
  ErrorState,
  Meter,
  Panel,
  Segmented,
  Select,
  Table,
  TextInput,
  useToast,
} from "@/components/ui";
import { EntityBadge } from "@/components/EntityBadge";
import { getCaseTimeline } from "@/lib/api";
import {
  confidencePct,
  formatTimestamp,
  truncate,
} from "@/lib/format";
import { labelFor } from "@/lib/entityTypes";
import type { Entity, TimelineEvent } from "@/lib/types";
import { apiCall, downloadBlob, toJson, useAsyncResource } from "@/lib/hooks";

/** Minimal structural types for vis — avoids importing its global namespace. */
interface VisItem {
  id: string;
  content: string;
  /** `point` for instants (OSINT events), `range` for spans. */
  type?: "point" | "range" | "box";
  start: number;
  group?: string;
  title?: string;
  className?: string;
  /** Inline CSS, used to tint each item with its source colour. */
  style?: string;
}

interface VisTimelineInstance {
  setItems: (items: VisItem[]) => void;
  setGroups: (groups: { id: string; content: string; className?: string }[]) => void;
  fit: (options?: { animation?: boolean }) => void;
  zoomIn: (factor?: number) => void;
  zoomOut: (factor?: number) => void;
  moveTo: (time: number, options?: { animation?: boolean }) => void;
  getWindow: () => { start: Date; end: Date };
  setOptions: (options: Record<string, unknown>) => void;
  destroy: () => void;
  on: (event: string, handler: (params: { item: string }) => void) => void;
  redraw: () => void;
}

export interface TimelineViewProps {
  /** Events from the current run's report. */
  events?: readonly TimelineEvent[];
  /** Entities that can be cross-linked from an event. */
  entities?: readonly Entity[];
  /** Case whose merged timeline is loaded when `events` is empty. */
  caseId?: string | null;
  /** Jump to the graph, focused on the given entity. */
  onOpenEntity?: (entity: Entity | null, entityId?: string) => void;
  className?: string;
}

/** Parse an event timestamp, falling back to the human date string. */
function eventTime(event: TimelineEvent): number | null {
  if (event.ts) {
    const parsed = Date.parse(event.ts);
    if (!Number.isNaN(parsed)) return parsed;
  }
  const parsed = Date.parse(event.date);
  return Number.isNaN(parsed) ? null : parsed;
}

/** Group colour by source, assigned deterministically. */
const GROUP_COLORS: Record<string, string> = {
  KRONOS: "#ffcc44",
  NEXUS: "#ff6644",
  SCOUT: "#00d4ff",
  SIGMA: "#ffb020",
  SENTINEL: "#ff4455",
  QUILL: "#ccddff",
  APEX: "#00ff88",
};

function groupClass(source: string): string {
  return `tl-group-${source.toLowerCase().replace(/[^a-z0-9]/g, "")}`;
}

/**
 * Inject the dark vis theme once.
 *
 * vis ships a light stylesheet; without this the items render as bright white
 * chips on the console. Everything here is derived from the same tokens the
 * rest of the app uses, so the timeline reads as part of the product.
 */
function installVisTheme(): void {
  if (typeof document === "undefined") return;
  const id = "signal-os-vis-theme";
  if (document.getElementById(id)) return;
  const style = document.createElement("style");
  style.id = id;
  style.textContent = `
    .vis-timeline { border: 1px solid #1e2d3d; background: #0d1117; font-family: var(--font-mono); }
    .vis-panel.vis-bottom, .vis-panel.vis-center, .vis-panel.vis-left, .vis-panel.vis-right,
    .vis-panel.vis-top, .vis-time-axis .vis-grid.vis-minor { border-color: #1e2d3d !important; }
    .vis-time-axis .vis-text { color: #5a7a9a; font-size: 9px; letter-spacing: 0.08em; }
    .vis-time-axis .vis-grid.vis-major { border-color: #2a4060; }
    .vis-labelset .vis-label { color: #c8d8e8; font-size: 10px; }
    .vis-foreground .vis-group { border-color: #2a4060; background: var(--line-hairline); }
    .vis-labelset .vis-label.vis-odd,
    .vis-labelset .vis-label.vis-even { background: transparent; color: #5a7a9a; }
    .vis-time-axis .vis-text.vis-vertical { color: #3d5872; }
    .vis-current-time { background-color: var(--line-accent-35); }

    /* Item chips: dark surface, mono type, per-source accent border. */
    .vis-item {
      border-radius: 3px;
      color: #c8d8e8 !important;
      font-family: var(--font-mono);
      font-size: 9.5px;
      padding: 1px 4px;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .vis-item .vis-item-content { color: #c8d8e8 !important; }
    .vis-item.vis-dot {
      border-width: 2px;
      border-style: solid;
      box-shadow: 0 0 5px currentColor;
    }
    .vis-item .vis-item-dot { border-color: inherit; }
    .vis-item.low { border-style: dashed; opacity: 0.75; }
    .vis-item.vis-selected {
      box-shadow: 0 0 0 1px #00d4ff, 0 0 10px var(--tint-info-45);
      color: #fff !important;
    }
    .vis-tooltip {
      background: #080c10;
      border: 1px solid #2a4060;
      color: #c8d8e8;
      font-size: 10px;
      font-family: var(--font-mono);
    }
  `;
  document.head.appendChild(style);
}

export interface TimelineFilterState {
  source: string;
  minConfidence: number;
  query: string;
}

/**
 * Chronology workbench.
 *
 * @example
 * <TimelineView events={report.timeline} entities={report.entities} onOpenEntity={focus} />
 */
export function TimelineView({
  events = [],
  entities = [],
  caseId = null,
  onOpenEntity,
  className,
}: TimelineViewProps) {
  const toast = useToast();
  const containerRef = useRef<HTMLDivElement>(null);
  const timelineRef = useRef<VisTimelineInstance | null>(null);
  /** Resize observers to disconnect when vis is rebuilt or unmounted. */
  const observersRef = useRef<ResizeObserver[]>([]);
  /**
   * `true` once the vis instance exists.
   *
   * The instance is created inside an async import, so the data-push effect can
   * run before it does. Gating on this flag (rather than only on `timelineRef`)
   * is what guarantees the first push happens *after* construction instead of
   * silently returning early and never re-running.
   */
  const [timelineReady, setTimelineReady] = useState(false);
  const [selected, setSelected] = useState<TimelineEvent | null>(null);
  const [filterSource, setFilterSource] = useState("");
  const [minConfidence, setMinConfidence] = useState(0);
  const [query, setQuery] = useState("");
  const [scale, setScale] = useState<"hours" | "days" | "months" | "years">("days");
  const [loadError, setLoadError] = useState<string | null>(null);

  const caseTimeline = useAsyncResource<TimelineEvent[]>(
    (signal) =>
      apiCall(
        () => getCaseTimeline(caseId as string, { signal, timeout_ms: 20_000 }),
        `/api/cases/${caseId}/timeline`,
      ),
    { enabled: !events.length && Boolean(caseId), deps: [caseId, events.length] },
  );

  /** All events: run payload first, otherwise the case timeline. */
  const allEvents = useMemo<TimelineEvent[]>(() => {
    if (events.length > 0) return [...events];
    return caseTimeline.data ?? [];
  }, [events, caseTimeline.data]);

  const sources = useMemo(() => {
    const set = new Set<string>();
    for (const event of allEvents) if (event.source) set.add(event.source);
    return [...set].sort();
  }, [allEvents]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return allEvents.filter((event) => {
      const time = eventTime(event);
      if (time === null) return false;
      if (filterSource && event.source !== filterSource) return false;
      if (typeof event.confidence === "number" && event.confidence < minConfidence) return false;
      if (!needle) return true;
      return (
        event.label.toLowerCase().includes(needle) ||
        (event.description ?? "").toLowerCase().includes(needle)
      );
    });
  }, [allEvents, filterSource, minConfidence, query]);

  /* ── Mount vis ── */
  useEffect(() => {
    let cancelled = false;

    const mount = async () => {
      const container = containerRef.current;
      if (!container || allEvents.length === 0) return;
      try {
        installVisTheme();
        const vis = await import("vis-timeline/standalone");
        if (cancelled || !containerRef.current) return;
        const ctor = (
          vis as unknown as {
            Timeline: new (
              container: HTMLElement,
              items: unknown,
              groups: unknown,
              options: Record<string, unknown>,
            ) => VisTimelineInstance;
          }
        ).Timeline;
        if (!ctor) throw new Error("vis-timeline did not export a Timeline constructor");

        const timeline = new ctor(container, [], [], {
          showCurrentTime: true,
          zoomMin: 1000 * 60,
          zoomMax: 1000 * 60 * 60 * 24 * 365 * 10,
          stack: false,
          orientation: { axis: "top", item: "top" },
          zoomable: true,
          moveable: true,
          selectable: true,
          multiselect: false,
          margin: { item: { horizontal: 6, vertical: 4 } },
          tooltip: { followMouse: true, overflowMethod: "cap" },
          // OSINT bursts land many events in the same minute; clustering keeps
          // a lane legible instead of stacking dozens of identical chips.
          cluster: {
            enabled: true,
            maxItemsInCluster: 6,
            clusterLabelTemplate: (params: { count: number }) => `${params.count} events`,
            clusterNestedGroupLabelTemplate: "",
            showGroupsAsNested: false,
            maxClusterLevel: 2,
            clusterLevelForItems: 1,
          },
          clusterInternalScaling: 0.7,
          clusterMaxWidth: 26,
        });
        timelineRef.current = timeline;
        setTimelineReady(true);
        timeline.on("itemclick", (params) => {
          // Plain items are `e<index>`; clusters are synthesised by vis and are
          // expanded by vis's own drill-down (zoom in to split them).
          const index = /^e(\d+)$/.exec(params.item)?.[1];
          if (index === undefined) return;
          const event = allEvents[Number(index)];
          if (event) setSelected(event);
        });

        // vis sizes its canvas from the container at construction time. Inside a
        // flex/grid column that measurement is 0 until after the first paint, so
        // without this the axis renders but no items ever get a lane.
        const ro = new ResizeObserver(() => {
          const host = containerRef.current;
          if (!host) return;
          const { width, height } = host.getBoundingClientRect();
          if (width > 0 && height > 0) {
            timeline.setOptions({ width: `${width}px`, height: `${height}px` });
            timeline.redraw();
          }
        });
        ro.observe(containerRef.current);
        observersRef.current.push(ro);

        // Apply the measured size once, up front — the observer only fires on
        // subsequent changes.
        const rect = containerRef.current.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
          timeline.setOptions({ width: `${rect.width}px`, height: `${rect.height}px` });
          timeline.redraw();
        }
      } catch (err) {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : String(err));
        }
      }
    };

    void mount();
    return () => {
      cancelled = true;
      setTimelineReady(false);
      for (const observer of observersRef.current) observer.disconnect();
      observersRef.current = [];
      timelineRef.current?.destroy();
      timelineRef.current = null;
    };
    // Mount once; data pushes happen in the effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allEvents.length > 0]);

  /* ── Push filtered data ── */
  useEffect(() => {
    const timeline = timelineRef.current;
    if (!timeline || !timelineReady) return;
    const grouped = new Set(filtered.map((event) => event.source ?? "unknown"));
    timeline.setGroups(
      [...grouped].map((source) => ({
        id: source,
        content: source,
        className: groupClass(source),
      })),
    );
    const items: VisItem[] = [];
    filtered.forEach((event, index) => {
      const time = eventTime(event);
      if (time === null) return;
      const color = GROUP_COLORS[(event.source ?? "").toUpperCase()] ?? "#5a7a9a";
      items.push({
        id: `e${index}`,
        // Every event is an instant, and bursts share a lane: draw the marker
        // only. The label lives in the tooltip, the side table and the drawer.
        content: "",
        type: "point",
        start: time,
        group: event.source ?? "unknown",
        title: `${event.label}${event.description ? `\n${event.description}` : ""}`,
        className: [
          groupClass(event.source ?? "unknown"),
          event.confidence !== undefined && event.confidence < 0.4 ? "low" : "",
        ]
          .filter(Boolean)
          .join(" "),
        // Inline so the accent wins over vis's default item background.
        style: `color:${color};background:${color}1f;border-color:${color}`,
      });
    });
    timeline.setItems(items);
    if (items.length > 0) timeline.fit({ animation: false });
  }, [filtered, filterSource, timelineReady]);

  /* ── Time-axis granularity ── */
  useEffect(() => {
    const timeline = timelineRef.current;
    if (!timeline || !timelineReady) return;
    const optionSets: Record<typeof scale, Record<string, unknown>> = {
      hours: { minorTimeLabels: { hour: "HH:mm" }, majorTimeLabels: { hour: "ddd D MMM" } },
      days: { minorTimeLabels: { day: "D MMM", hour: "HH:mm" }, majorTimeLabels: { day: "MMMM YYYY" } },
      months: { minorTimeLabels: { month: "MMM" }, majorTimeLabels: { year: "YYYY" } },
      years: { minorTimeLabels: {}, majorTimeLabels: { year: "YYYY" } },
    };
    timeline.setOptions({ ...optionSets[scale] });
  }, [scale, timelineReady]);

  const zoomIn = useCallback(() => timelineRef.current?.zoomIn(1.4), []);
  const zoomOut = useCallback(() => timelineRef.current?.zoomOut(1.4), []);
  const fitAll = useCallback(() => timelineRef.current?.fit({ animation: true }), []);

  const exportJson = useCallback(() => {
    downloadBlob(
      toJson(filtered),
      `signal-os-timeline-${caseId ?? "run"}.json`,
      "application/json",
    );
    toast.success("Timeline exported.");
  }, [caseId, filtered, toast]);

  const exportCsv = useCallback(() => {
    const header = "ts,date,label,description,source,confidence,entities";
    const rows = filtered.map((event) =>
      [
        event.ts ?? "",
        event.date,
        csvCell(event.label),
        csvCell(event.description ?? ""),
        csvCell(event.source ?? ""),
        typeof event.confidence === "number" ? String(event.confidence) : "",
        csvCell((event.entities ?? []).join(" ")),
      ].join(","),
    );
    downloadBlob([header, ...rows].join("\n"), `signal-os-timeline-${caseId ?? "run"}.csv`, "text/csv");
    toast.success("Timeline CSV exported.");
  }, [caseId, filtered, toast]);

  const linkedEntities = useMemo(() => {
    if (!selected?.entities?.length) return [];
    const ids = new Set(selected.entities);
    const direct = entities.filter((entity) => ids.has(entity.id));
    if (direct.length > 0) return direct;
    // Fall back to a value match when the ids differ from our entity list.
    const labels = new Set(
      selected.entities.map((id) => id.split(":").slice(1).join(":").toLowerCase()),
    );
    return entities.filter((entity) =>
      labels.has((entity.value ?? entity.label).toLowerCase()),
    );
  }, [entities, selected]);

  return (
    <div className={clsx("flex min-h-0 flex-1 flex-col", className)}>
      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-1 px-3 py-1.5">
        <div className="min-w-[180px] flex-1">
          <TextInput
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search events…"
            aria-label="Search timeline events"
            className="!py-1 !text-[11px]"
          />
        </div>
        <Select
          value={filterSource}
          onChange={(event) => setFilterSource(event.target.value)}
          aria-label="Filter by source"
          className="!w-[150px] !py-1 !text-[10px]"
        >
          <option value="">ALL SOURCES</option>
          {sources.map((source) => (
            <option key={source} value={source}>
              {source}
            </option>
          ))}
        </Select>
        <label className="flex items-center gap-1.5">
          <span className="mono-label !text-[8px]">MIN CONF</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={minConfidence}
            onChange={(event) => setMinConfidence(Number(event.target.value))}
            aria-label="Minimum confidence"
            className="focus-ring w-24 accent-[var(--green)]"
          />
          <span className="w-8 text-[9px] tabular-nums text-ink-muted">
            {Math.round(minConfidence * 100)}%
          </span>
        </label>
        <Segmented
          label="Time granularity"
          value={scale}
          onChange={setScale}
          options={[
            { value: "hours", label: "H" },
            { value: "days", label: "D" },
            { value: "months", label: "M" },
            { value: "years", label: "Y" },
          ]}
        />
        <Button size="sm" variant="outline" onClick={zoomIn} iconLabel="Zoom in">
          +
        </Button>
        <Button size="sm" variant="outline" onClick={zoomOut} iconLabel="Zoom out">
          −
        </Button>
        <Button size="sm" variant="outline" onClick={fitAll}>
          Fit
        </Button>
        <Button size="sm" variant="ghost" onClick={exportCsv}>
          CSV
        </Button>
        <Button size="sm" variant="ghost" onClick={exportJson}>
          JSON
        </Button>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 overflow-hidden p-2 xl:grid-rows-1 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="flex h-full min-h-[360px] min-w-0 flex-col gap-2">
          {loadError ? (
            <Panel>
              <ErrorState
                message={`The timeline engine failed to load: ${loadError}`}
                title="Timeline unavailable"
              />
            </Panel>
          ) : allEvents.length === 0 ? (
            <div className="flex min-h-[360px] flex-1 items-center justify-center rounded-md border border-border-subtle bg-surface-1">
              {caseTimeline.state === "loading" ? (
                <EmptyState
                  compact
                  glyph="◷"
                  title="LOADING TIMELINE…"
                  description="Fetching case timeline events. They appear here once the case has investigations with dated signals."
                />
              ) : caseTimeline.state === "error" ? (
                <ErrorState compact message={caseTimeline.error} onRetry={caseTimeline.reload} />
              ) : (
                <EmptyState
                  glyph="⊕"
                  title="NO TIMELINE EVENTS"
                  description={
                    caseId
                      ? "This case has no dated events yet."
                      : "KRONOS reconstructs a chronology once a run produces dated signals."
                  }
                />
              )}
            </div>
          ) : (
            <>
              <div
                ref={containerRef}
                role="application"
                aria-label="Event timeline. Drag to pan, scroll to zoom."
                className="h-full min-h-[320px] flex-1 rounded-md"
              />
              <p className="mono-label !text-[8px]">
                {filtered.length} OF {allEvents.length} EVENTS · CLICK AN EVENT FOR DETAIL
              </p>
            </>
          )}
        </div>

        <aside className="scroll-thin flex min-h-0 flex-col gap-2 overflow-y-auto">
          <Panel
            eyebrow="KRONOS"
            title="Chronology"
            actions={<Badge tone="idle">{filtered.length}</Badge>}
            bodyClassName="!p-0"
          >
            <Table
              columns={[
                {
                  key: "ts",
                  header: "When",
                  width: "128px",
                  sortValue: (row) => eventTime(row) ?? 0,
                  render: (row) => (
                    <span className="mono-label !text-[9px]">
                      {row.ts ? formatTimestamp(row.ts).slice(5, 16) : truncate(row.date, 16)}
                    </span>
                  ),
                },
                {
                  key: "label",
                  header: "Event",
                  render: (row) => (
                    <span className="block min-w-0 truncate" title={row.label}>
                      {truncate(row.label, 52)}
                    </span>
                  ),
                },
                {
                  key: "source",
                  header: "Source",
                  width: "96px",
                  hideBelow: "md",
                  sortValue: (row) => row.source ?? "",
                  render: (row) => (
                    <span className="mono-label !text-[9px]">{row.source ?? "—"}</span>
                  ),
                },
                {
                  key: "confidence",
                  header: "C",
                  width: "52px",
                  align: "right",
                  hideBelow: "lg",
                  sortValue: (row) => row.confidence ?? -1,
                  render: (row) => (
                    <span className="tabular-nums">
                      {row.confidence === undefined ? "—" : confidencePct(row.confidence)}
                    </span>
                  ),
                },
              ]}
              rows={filtered}
              rowKey={(row, index) => `${row.id}-${index}`}
              onRowClick={setSelected}
              isRowActive={(row) => row.id === selected?.id}
              maxHeight="calc(100vh - 260px)"
              caption="Timeline events"
              defaultSort={{ key: "ts", direction: "desc" }}
              empty={
                <EmptyState
                  compact
                  title="NO EVENTS MATCH"
                  description="Relax the source, confidence or text filter."
                />
              }
            />
          </Panel>
        </aside>
      </div>

      {/* Event detail */}
      <Drawer
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected ? truncate(selected.label, 48) : ""}
        width={380}
      >
        {selected && (
          <div className="flex flex-col gap-3 p-3">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone="info">{selected.source ?? "unknown"}</Badge>
              {selected.ts && <Badge tone="idle">{formatTimestamp(selected.ts)}</Badge>}
              {selected.confidence !== undefined && (
                <Badge tone={selected.confidence >= 0.6 ? "ok" : "warn"}>
                  {confidencePct(selected.confidence)} confidence
                </Badge>
              )}
            </div>

            {selected.confidence !== undefined && (
              <Meter
                value={selected.confidence}
                tone={selected.confidence >= 0.6 ? "ok" : "warn"}
                label="Event confidence"
              />
            )}

            {selected.description && (
              <p className="m-0 whitespace-pre-wrap text-[11px] leading-relaxed text-ink-muted">
                {selected.description}
              </p>
            )}

            <div>
              <DefRow label="Id" mono>
                {selected.id}
              </DefRow>
              <DefRow label="Date" mono>
                {selected.date}
              </DefRow>
              {selected.ts && <DefRow label="Timestamp" mono>{selected.ts}</DefRow>}
            </div>

            <div>
              <div className="mono-label mb-1 !text-[9px]">
                LINKED ENTITIES ({linkedEntities.length})
              </div>
              {linkedEntities.length === 0 ? (
                <EmptyState
                  compact
                  glyph="◌"
                  title="NO LINKED ENTITIES"
                  description={
                    (selected.entities?.length ?? 0) > 0
                      ? "This event references entity ids that are not in the current run's entity list."
                      : "KRONOS did not attach entities to this event."
                  }
                />
              ) : (
                <ul className="flex list-none flex-col gap-1 p-0">
                  {linkedEntities.map((entity) => (
                    <li key={entity.id} className="flex items-center gap-2">
                      <EntityBadge type={entity.type} showLabel={false} />
                      <button
                        type="button"
                        onClick={() => onOpenEntity?.(entity, entity.id)}
                        className="focus-ring min-w-0 flex-1 truncate rounded text-left text-[10px] text-ink hover:text-accent"
                        title="Open in graph"
                      >
                        {truncate(entity.label || entity.value || entity.id, 44)}
                      </button>
                      <span className="mono-label shrink-0 !text-[8px]">
                        {labelFor(entity.type)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={linkedEntities.length === 0}
                onClick={() => onOpenEntity?.(linkedEntities[0] ?? null, linkedEntities[0]?.id)}
              >
                Open first entity in graph ↗
              </Button>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}

/** Escape one CSV cell. */
function csvCell(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}
