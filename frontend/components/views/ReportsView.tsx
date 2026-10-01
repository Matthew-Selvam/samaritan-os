"use client";

/**
 * ReportsView.tsx — QUILL report rendering and export.
 *
 * Renders report markdown through `react-markdown` + `remark-gfm` with proper
 * typography (headings, tables, blockquotes, code, task lists) styled to match
 * the console, plus a table of contents, the agent-contribution breakdown,
 * confidence display and export in md/json/pdf.
 *
 * PDF export prefers the backend's rendered artefact and falls back to the
 * browser print dialog with a print stylesheet — the report always leaves the
 * machine, one way or the other.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Badge,
  Button,
  DefRow,
  EmptyState,
  ErrorState,
  Meter,
  Panel,
  SectionTitle,
  Segmented,
  SkeletonRows,
  StatTile,
  Select,
  useToast,
} from "@/components/ui";
import { EntityBadge } from "@/components/EntityBadge";
import { getReport, downloadReport } from "@/lib/api";
import {
  confidencePct,
  formatRelativeTime,
  formatSeconds,
  truncate,
  type Tone,
} from "@/lib/format";
import type {
  AgentResult,
  Entity,
  Investigation,
  Report,
  ReportFormat,
} from "@/lib/types";
import { apiCall, copyToClipboard, downloadBlob, useAsyncResource } from "@/lib/hooks";

export interface ReportsViewProps {
  investigation: Investigation | null;
  /** Runs available to pick from. */
  available?: readonly Investigation[];
  onSelect?: (investigation: Investigation) => void;
  className?: string;
}

/** One markdown heading, used to build the table of contents. */
interface TocEntry {
  id: string;
  text: string;
  level: number;
}

/** Slugify a heading into a stable DOM id. */
function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}

/** Confidence band → tone. */
function confidenceTone(confidence: number | undefined): Tone {
  if (confidence === undefined) return "idle";
  if (confidence >= 0.75) return "ok";
  if (confidence >= 0.45) return "warn";
  return "err";
}

/** Extract headings from markdown for the TOC. */
function extractToc(markdown: string): TocEntry[] {
  const out: TocEntry[] = [];
  const seen = new Map<string, number>();
  for (const line of markdown.split("\n")) {
    const match = /^(#{1,4})\s+(.+?)\s*#*\s*$/.exec(line);
    if (!match) continue;
    const text = match[2].replace(/[*_`]/g, "").trim();
    if (!text) continue;
    const base = slugify(text);
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    out.push({ id: count === 0 ? base : `${base}-${count}`, text, level: match[1].length });
  }
  return out;
}

/**
 * Report workbench.
 *
 * @example
 * <ReportsView investigation={inv} available={history} onSelect={openRun} />
 */
export function ReportsView({
  investigation,
  available = [],
  onSelect,
  className,
}: ReportsViewProps) {
  const toast = useToast();
  const bodyRef = useRef<HTMLDivElement>(null);
  const [markdown, setMarkdown] = useState<string>("");
  const [toc, setToc] = useState<TocEntry[]>([]);
  const [busyFormat, setBusyFormat] = useState<ReportFormat | null>(null);
  const [density, setDensity] = useState<"comfortable" | "compact">("comfortable");

  const invId = investigation?.inv_id ?? null;
  const inlineReport: Report | null = investigation?.report ?? null;

  /* ── Prefer the inline markdown; otherwise ask the backend ── */
  const remote = useAsyncResource<Report | string>(
    async (signal) => {
      const payload = await apiCall(
        () => getReport<unknown>(invId as string, "markdown", { signal, timeout_ms: 45_000 }),
        `/api/investigate/${invId}/report`,
      );
      if (typeof payload === "string") return payload;
      if (payload && typeof payload === "object") {
        const record = payload as { markdown?: string; content?: string };
        if (typeof record.markdown === "string") return record.markdown;
        if (typeof record.content === "string") return record.content;
      }
      return "";
    },
    { enabled: Boolean(invId) && !inlineReport?.markdown, deps: [invId, inlineReport?.markdown] },
  );

  /* Fallback markdown when the backend produced no `report_markdown`. */
  const synthesised = useMemo(() => {
    if (!investigation) return "";
    const report = inlineReport;
    const lines: string[] = [];
    lines.push(`# Investigation ${investigation.inv_id}`, "");
    lines.push(`**Target:** \`${investigation.input}\`  `);
    lines.push(`**Type:** ${investigation.input_type}  `);
    lines.push(`**Status:** ${investigation.status}  `);
    lines.push(`**Started:** ${investigation.started_at ?? "—"}`, "");
    if (investigation.routing_reasoning) {
      lines.push("## Routing", "", investigation.routing_reasoning, "");
    }
    if (report?.summary) {
      lines.push("## Summary", "", report.summary, "");
    }
    if (report?.confidence !== undefined) {
      lines.push(`**Report confidence:** ${confidencePct(report.confidence)}`, "");
    }
    const entities = report?.entities ?? [];
    if (entities.length > 0) {
      lines.push("## Entities", "", "| Type | Label | Confidence | Source |", "| --- | --- | --- | --- |");
      for (const entity of entities.slice(0, 200)) {
        lines.push(
          `| ${entity.type} | ${escapeCell(entity.label || entity.value || entity.id)} | ${confidencePct(entity.confidence)} | ${entity.source ?? "—"} |`,
        );
      }
      lines.push("");
    }
    const timeline = report?.timeline ?? [];
    if (timeline.length > 0) {
      lines.push("## Timeline", "", "| When | Event | Source |", "| --- | --- | --- |");
      for (const event of timeline.slice(0, 100)) {
        lines.push(
          `| ${event.ts ?? event.date} | ${escapeCell(event.label)} | ${event.source ?? "—"} |`,
        );
      }
      lines.push("");
    }
    if (investigation.agents_activated?.length) {
      lines.push("## Agents activated", "");
      for (const agent of investigation.agents_activated) lines.push(`- ${agent}`);
      lines.push("");
    }
    return lines.join("\n");
  }, [investigation, inlineReport]);

  const activeMarkdown = useMemo(() => {
    const candidates = [
      inlineReport?.markdown,
      typeof remote.data === "string" ? remote.data : inlineReport?.markdown,
      synthesised,
    ];
    return candidates.find((value): value is string => Boolean(value && value.trim())) ?? "";
  }, [inlineReport?.markdown, remote.data, synthesised]);

  useEffect(() => {
    setMarkdown(activeMarkdown);
    setToc(extractToc(activeMarkdown));
  }, [activeMarkdown]);

  /* ── Agent contribution ── */
  const contributions = useMemo(() => {
    const results: readonly AgentResult[] = investigation?.agents ?? [];
    if (results.length === 0) {
      return (investigation?.agents_activated ?? []).map((name) => ({
        agent: name,
        status: "skipped" as const,
        confidence: undefined,
        latency_s: undefined,
        entities: 0,
        signals: 0,
      }));
    }
    return results.map((result) => ({
      agent: result.agent,
      status: result.status,
      confidence: result.confidence,
      latency_s: result.latency_s,
      entities: result.entities_found?.length ?? 0,
      signals: result.signals?.length ?? 0,
    }));
  }, [investigation]);

  const totalEntities = contributions.reduce((sum, c) => sum + c.entities, 0);
  const totalSignals = contributions.reduce((sum, c) => sum + c.signals, 0);

  /* ── Copy / export ── */
  const copyMarkdown = useCallback(async () => {
    if (!markdown) {
      toast.error("There is no report to copy yet.");
      return;
    }
    const ok = await copyToClipboard(markdown);
    if (ok) toast.success("Report markdown copied.");
    else toast.error("Clipboard access was refused by the browser.");
  }, [markdown, toast]);

  const exportMarkdown = useCallback(() => {
    if (!markdown) return;
    downloadBlob(markdown, `signal-os-report-${invId ?? "run"}.md`, "text/markdown");
    toast.success("Markdown downloaded.");
  }, [invId, markdown, toast]);

  const exportJson = useCallback(() => {
    if (!investigation) return;
    downloadBlob(
      JSON.stringify(
        {
          investigation: {
            inv_id: investigation.inv_id,
            input: investigation.input,
            input_type: investigation.input_type,
            status: investigation.status,
            started_at: investigation.started_at,
            ended_at: investigation.ended_at,
          },
          report: investigation.report ?? null,
          agents: investigation.agents ?? [],
          markdown,
        },
        null,
        2,
      ),
      `signal-os-report-${investigation.inv_id}.json`,
      "application/json",
    );
    toast.success("JSON bundle downloaded.");
  }, [investigation, markdown, toast]);

  const exportPdf = useCallback(async () => {
    if (!invId) return;
    setBusyFormat("pdf");
    try {
      const buffer = await downloadReport(invId, "pdf", { timeout_ms: 90_000 });
      downloadBlob(
        new Uint8Array(buffer),
        `signal-os-report-${invId}.pdf`,
        "application/pdf",
      );
      toast.success("PDF downloaded from the backend renderer.");
    } catch (err) {
      // The backend may not render PDFs; the print stylesheet always works.
      console.warn("[signal-os] backend PDF export failed, falling back to print:", err);
      toast.push(
        "Backend PDF export unavailable — opening the print dialog instead.",
        "warn",
      );
      window.setTimeout(() => window.print(), 400);
    } finally {
      setBusyFormat(null);
    }
  }, [invId, toast]);

  const scrollToHeading = useCallback((id: string) => {
    const target = bodyRef.current?.querySelector(`#${CSS.escape(id)}`);
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  const reportEntities: readonly Entity[] = inlineReport?.entities ?? [];

  if (!investigation) {
    return (
      <div className={clsx("scroll-thin min-h-0 flex-1 overflow-y-auto p-3", className)}>
        {available.length > 0 ? (
          <Panel eyebrow="REPORTS" title="Choose a run">
            <ul className="flex list-none flex-col gap-1 p-0">
              {available.slice(0, 30).map((inv) => (
                <li key={inv.inv_id}>
                  <button
                    type="button"
                    onClick={() => onSelect?.(inv)}
                    className="focus-ring flex w-full flex-col gap-0.5 rounded border border-border-subtle bg-surface-2 px-2 py-1.5 text-left transition-colors hover:border-border-strong"
                  >
                    <span className="truncate text-[11px] text-ink">{inv.input}</span>
                    <span className="mono-label !text-[8px]">
                      {inv.inv_id} · {inv.status} ·{" "}
                      {inv.started_at ? formatRelativeTime(inv.started_at) : "—"}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </Panel>
        ) : (
          <EmptyState
            glyph="⟁"
            title="NO REPORTS"
            description="Reports appear once QUILL has synthesised a run. Run an investigation to generate one."
          />
        )}
      </div>
    );
  }

  return (
    <div className={clsx("flex min-h-0 flex-1 flex-col", className)}>
      {/* Header */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-1 px-3 py-1.5">
        <Badge tone={statusToneFor(investigation.status)}>
          {investigation.status.toUpperCase()}
        </Badge>
        <span className="mono-label min-w-0 truncate !text-[9px]">
          {truncate(investigation.input, 44)} · {investigation.inv_id}
        </span>
        {available.length > 1 && (
          <Select
            aria-label="Choose a run"
            value={investigation.inv_id}
            onChange={(event) => {
              const next = available.find((inv) => inv.inv_id === event.target.value);
              if (next) onSelect?.(next);
            }}
            className="!w-[190px] !py-0.5 !text-[10px]"
          >
            {available.map((inv) => (
              <option key={inv.inv_id} value={inv.inv_id}>
                {truncate(inv.input, 30)} · {inv.inv_id}
              </option>
            ))}
          </Select>
        )}
        <span className="flex-1" />
        <Segmented
          label="Report density"
          value={density}
          onChange={setDensity}
          options={[
            { value: "comfortable", label: "Comfortable" },
            { value: "compact", label: "Compact" },
          ]}
        />
        <Button size="sm" variant="outline" onClick={() => void copyMarkdown()} disabled={!markdown}>
          Copy
        </Button>
        <Button size="sm" variant="ghost" onClick={exportMarkdown} disabled={!markdown}>
          MD
        </Button>
        <Button size="sm" variant="ghost" onClick={exportJson}>
          JSON
        </Button>
        <Button
          size="sm"
          variant="solid"
          loading={busyFormat === "pdf"}
          onClick={() => void exportPdf()}
        >
          PDF
        </Button>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 overflow-y-auto p-2 xl:grid-cols-[220px_minmax(0,1fr)_300px]">
        {/* TOC */}
        <aside className="scroll-thin hidden min-h-0 flex-col gap-2 overflow-y-auto xl:flex">
          <Panel eyebrow="CONTENTS" title="Table of contents">
            {toc.length === 0 ? (
              <EmptyState
              compact
              glyph="⋯"
              title="NO HEADINGS"
              description="The table of contents is built from the report's Markdown headings. QUILL emits them once a report has been written."
            />
            ) : (
              <nav aria-label="Report contents">
                <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
                  {toc.map((entry) => (
                    <li key={entry.id} style={{ paddingLeft: (entry.level - 1) * 10 }}>
                      <button
                        type="button"
                        onClick={() => scrollToHeading(entry.id)}
                        className="focus-ring w-full truncate rounded text-left text-[10px] text-ink-muted transition-colors hover:text-accent"
                        title={entry.text}
                      >
                        {entry.text}
                      </button>
                    </li>
                  ))}
                </ul>
              </nav>
            )}
          </Panel>

          <Panel eyebrow="STATS" title="Run facts">
            <StatTile
              label="Confidence"
              value={
                inlineReport?.confidence === undefined ? "—" : confidencePct(inlineReport.confidence)
              }
              tone={confidenceTone(inlineReport?.confidence)}
              progress={inlineReport?.confidence}
              hint="Overall report confidence"
            />
            <div className="mt-2">
              <DefRow label="Latency">
                {formatSeconds(investigation.ended_at && investigation.started_at
                  ? (Date.parse(investigation.ended_at) - Date.parse(investigation.started_at)) / 1000
                  : inlineReport?.latency_s)}
              </DefRow>
              <DefRow label="Entities">{reportEntities.length}</DefRow>
              <DefRow label="Signals">{inlineReport?.signals?.length ?? 0}</DefRow>
              <DefRow label="Timeline">{inlineReport?.timeline?.length ?? 0}</DefRow>
              <DefRow label="Agents">{contributions.length}</DefRow>
            </div>
          </Panel>
        </aside>

        {/* Rendered report */}
        <div className="flex min-h-0 flex-col">
          {remote.state === "loading" && !markdown ? (
            <Panel eyebrow="QUILL" title="Rendering report">
              <SkeletonRows rows={8} height={18} />
            </Panel>
          ) : !markdown ? (
            <Panel eyebrow="QUILL" title="Report">
              <EmptyState
                glyph="⟁"
                title="NO REPORT CONTENT"
                description={
                  remote.state === "error"
                    ? "The report endpoint failed and this run carried no inline markdown."
                    : "This run finished without a markdown report."
                }
                action={
                  remote.state === "error" ? (
                    <Button variant="outline" onClick={remote.reload}>
                      Retry
                    </Button>
                  ) : undefined
                }
              />
              {remote.state === "error" && (
                <ErrorState compact message={remote.error} onRetry={remote.reload} />
              )}
            </Panel>
          ) : (
            <Panel
              eyebrow="QUILL"
              title="Intelligence report"
              actions={
                <span className="mono-label !text-[8px]">
                  {markdown.length.toLocaleString()} CHARS
                </span>
              }
              bodyClassName={clsx(
                "report-body",
                density === "compact" && "report-body--compact",
              )}
            >
              <div ref={bodyRef}>
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    h1: ({ children, ...rest }) => (
                      <h1 id={slugify(flatten(children))} {...rest}>
                        {children}
                      </h1>
                    ),
                    h2: ({ children, ...rest }) => (
                      <h2 id={slugify(flatten(children))} {...rest}>
                        {children}
                      </h2>
                    ),
                    h3: ({ children, ...rest }) => (
                      <h3 id={slugify(flatten(children))} {...rest}>
                        {children}
                      </h3>
                    ),
                    h4: ({ children, ...rest }) => (
                      <h4 id={slugify(flatten(children))} {...rest}>
                        {children}
                      </h4>
                    ),
                    a: ({ children, href, ...rest }) => {
                      const safe = safeHref(href);
                      return (
                        <a
                          href={safe ?? "#"}
                          {...(safe
                            ? { target: "_blank", rel: "noopener noreferrer" }
                            : {})}
                          {...rest}
                        >
                          {children}
                          {safe ? " ↗" : ""}
                        </a>
                      );
                    },
                    // Callouts: blockquotes styled as intel callouts.
                    blockquote: ({ children, ...rest }) => (
                      <blockquote className="report-callout" {...rest}>
                        {children}
                      </blockquote>
                    ),
                    table: ({ children, ...rest }) => (
                      <div className="scroll-thin report-table-wrap">
                        <table {...rest}>{children}</table>
                      </div>
                    ),
                    code: ({ children, className, ...rest }) => {
                      const isBlock = Boolean(className?.includes("language-"));
                      return isBlock ? (
                        <code className={clsx("report-code", className)} {...rest}>
                          {children}
                        </code>
                      ) : (
                        <code className="report-inline-code" {...rest}>
                          {children}
                        </code>
                      );
                    },
                  }}
                >
                  {markdown}
                </ReactMarkdown>
              </div>
            </Panel>
          )}
        </div>

        {/* Contributions */}
        <aside className="scroll-thin flex min-h-0 flex-col gap-2 overflow-y-auto">
          <Panel
            eyebrow="CONTRIBUTION"
            title="Agent breakdown"
            actions={
              <>
                <Badge tone="info">{totalEntities} ENT</Badge>
                <Badge tone="warn">{totalSignals} SIG</Badge>
              </>
            }
            bodyClassName="!p-0"
          >
            {contributions.length === 0 ? (
              <EmptyState
                compact
                glyph="◌"
                title="NO AGENT BREAKDOWN"
                description="This run did not report per-agent contributions."
              />
            ) : (
              <ul className="flex list-none flex-col gap-1 p-2">
                {contributions.map((entry) => (
                  <li
                    key={entry.agent}
                    className="flex items-center gap-2 rounded border border-border-subtle bg-surface-2 px-2 py-1"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[10px] font-bold tracking-wide text-ink">
                        {entry.agent}
                      </span>
                      {typeof entry.confidence === "number" && (
                        <Meter
                          value={entry.confidence}
                          tone={confidenceTone(entry.confidence)}
                          height={3}
                          className="mt-0.5"
                          label={`${entry.agent} confidence`}
                        />
                      )}
                    </span>
                    <Badge
                      tone={
                        entry.status === "done"
                          ? "ok"
                          : entry.status === "partial"
                            ? "warn"
                            : entry.status === "error"
                              ? "err"
                              : "idle"
                      }
                    >
                      {entry.status === "skipped" ? "N/A" : entry.status.slice(0, 4).toUpperCase()}
                    </Badge>
                    <span className="mono-label shrink-0 !text-[8px] tabular-nums">
                      {formatSeconds(entry.latency_s)}
                    </span>
                    <span className="w-14 shrink-0 text-right text-[9px] tabular-nums text-ink-muted">
                      {entry.entities}e / {entry.signals}s
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          {reportEntities.length > 0 && (
            <Panel eyebrow="EVIDENCE" title={`${reportEntities.length} entities`} bodyClassName="!p-0">
              <div className="scroll-thin max-h-[320px] overflow-y-auto">
                <ul className="flex list-none flex-col">
                  {reportEntities.slice(0, 200).map((entity) => (
                    <li
                      key={entity.id}
                      className="flex items-center gap-2 border-b border-border-subtle/60 px-2 py-1"
                    >
                      <EntityBadge type={entity.type} showLabel={false} />
                      <span className="min-w-0 flex-1 truncate text-[10px] text-ink">
                        {truncate(entity.label || entity.value || entity.id, 44)}
                      </span>
                      <span className="mono-label shrink-0 !text-[8px]">
                        {confidencePct(entity.confidence)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            </Panel>
          )}
        </aside>
      </div>
    </div>
  );
}

/** Tone for an investigation status badge. */
function statusToneFor(status: string): Tone {
  if (status === "done") return "ok";
  if (status === "error") return "err";
  if (status === "running") return "info";
  if (status === "routing") return "warn";
  return "idle";
}

/** Flatten React children into a string for slug generation. */
function flatten(children: unknown): string {
  if (typeof children === "string") return children;
  if (typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(flatten).join("");
  if (children && typeof children === "object" && "props" in children) {
    return flatten((children as { props?: { children?: unknown } }).props?.children);
  }
  return "";
}

/** Reject javascript:/data: URLs in rendered markdown links. */
function safeHref(href: string | undefined): string | null {
  if (!href) return null;
  try {
    const url = new URL(href, "https://signal-os.local");
    if (url.protocol === "http:" || url.protocol === "https:") return href;
    return null;
  } catch {
    return null;
  }
}

/** Escape a markdown table cell. */
function escapeCell(value: string): string {
  return value.replace(/\|/g, "\\|").replace(/\n/g, " ");
}
