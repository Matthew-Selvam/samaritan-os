"use client";

/**
 * PipelineTrace.tsx — the live pipeline log for one investigation.
 *
 * Reuses the original step colouriser (`colorizeStep`, preserved verbatim below)
 * so a step line looks the same wherever it appears. `formatDuration` from
 * `lib/format.ts` replaces the old epoch arithmetic, and `statusTone` replaces
 * the local status palette.
 */

import { useEffect, useRef } from "react";
import { Badge, Panel } from "./ui";
import {
  confidencePct,
  formatDuration,
  statusLabel,
  statusTone,
} from "@/lib/format";
import { colorFor } from "@/lib/entityTypes";
import type { Investigation } from "@/lib/types";

/** Input-type accent colours, carried over from the original component. */
const INPUT_TYPE_COLORS: Record<string, string> = {
  email: "#00d4ff",
  domain: "#00ff88",
  ip_address: "#ffb020",
  username: "#9966ff",
  crypto_wallet: "#ff8833",
  url: "#44dd88",
  image: "#ff66aa",
  video: "#ff66aa",
  audio: "#ffb020",
  document: "#aabbff",
  phone: "#ffcc44",
  text: "#c8d8e8",
  unknown: "#5a7a9a",
};

/** Accent colour for a raw input-type string from the router. */
export function inputTypeColor(inputType: string | null | undefined): string {
  if (!inputType) return INPUT_TYPE_COLORS.unknown;
  return INPUT_TYPE_COLORS[inputType] ?? colorFor(inputType);
}

export interface PipelineTraceProps {
  investigation: Investigation;
  /** `true` to follow the log tail as new steps arrive. */
  autoScroll?: boolean;
  className?: string;
}

/** Elapsed milliseconds for a run, whichever timestamps are available. */
export function runElapsedMs(inv: Investigation, now = Date.now()): number {
  if (typeof inv.started_at !== "string") return 0;
  const start = Date.parse(inv.started_at);
  if (Number.isNaN(start)) return 0;
  const end = typeof inv.ended_at === "string" ? Date.parse(inv.ended_at) : now;
  if (Number.isNaN(end)) return 0;
  return Math.max(0, end - start);
}

/**
 * Run header + ordered pipeline trace.
 *
 * The log is a `role="log"` region with `aria-live="polite"`, so steps are
 * announced as they arrive without stealing focus.
 */
export function PipelineTrace({
  investigation,
  autoScroll = true,
  className,
}: PipelineTraceProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const steps = investigation.steps ?? [];
  const live = investigation.status === "routing" || investigation.status === "running";

  useEffect(() => {
    if (!autoScroll) return;
    bottomRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [steps.length, autoScroll]);

  const typeColor = inputTypeColor(investigation.input_type);
  const elapsed = formatDuration(runElapsedMs(investigation));
  const agents = investigation.agents_activated ?? [];

  return (
    <div className={`flex flex-col gap-3 ${className ?? ""}`} style={{ animation: "slide-in 0.2s ease-out" }}>
      {/* Run header */}
      <Panel
        eyebrow="RUN"
        title={
          <span className="truncate" title={investigation.input}>
            {investigation.input}
          </span>
        }
        actions={
          <>
            <Badge
              color={typeColor}
              title={`Detected input type: ${investigation.input_type}`}
            >
              {investigation.input_type || "unknown"}
            </Badge>
            <Badge tone={statusTone(investigation.status)} pulse={live}>
              {statusLabel(investigation.status)}
            </Badge>
            <Badge tone="idle" title="Wall-clock duration of this run">
              {elapsed}
            </Badge>
            {investigation.routing_confidence !== undefined && (
              <Badge tone="ok" title="APEX routing confidence">
                {confidencePct(investigation.routing_confidence)}
              </Badge>
            )}
          </>
        }
      >
        <div className="flex flex-col gap-2">
          {agents.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="mono-label !text-[8px]">AGENTS</span>
              {agents.map((agent) => (
                <Badge key={agent} tone="ok" size="sm">
                  {agent}
                </Badge>
              ))}
            </div>
          )}

          {investigation.routing_reasoning && (
            <p className="m-0 text-[10px] leading-relaxed text-ink-muted">
              <span className="mono-label mr-1 !text-[8px]">ROUTING</span>
              {investigation.routing_reasoning}
            </p>
          )}

          {investigation.error && (
            <p role="alert" className="m-0 break-words text-[10px] text-signal-err">
              {investigation.error}
            </p>
          )}
        </div>
      </Panel>

      {/* Trace log */}
      <Panel
        eyebrow="TRACE"
        title={`${steps.length} step${steps.length === 1 ? "" : "s"}`}
        actions={<Badge tone={live ? "info" : "idle"} pulse={live}>{live ? "STREAMING" : "COMPLETE"}</Badge>}
        bodyClassName="!p-0"
      >
        <div
          role="log"
          aria-live="polite"
          aria-label="Pipeline trace log"
          className="scroll-thin max-h-[52vh] min-h-[120px] overflow-y-auto px-3 py-2"
        >
          {steps.length === 0 && !live && (
            <p className="mono-label m-0 py-2">NO STEPS RECORDED</p>
          )}

          {steps.map((step, index) => (
            <div
              key={`${index}-${step.slice(0, 24)}`}
              className="flex gap-3 py-[1px]"
              style={{ animation: "slide-in 0.15s ease-out" }}
            >
              <span aria-hidden="true" className="w-7 shrink-0 text-[10px] text-ink-muted tabular-nums">
                {String(index + 1).padStart(2, "0")}
              </span>
              <span aria-hidden="true" className="shrink-0 text-[10px] text-accent">
                ›
              </span>
              <span className="min-w-0 break-words text-[11px]" style={{ color: colorizeStep(step) }}>
                {step}
              </span>
            </div>
          ))}

          {live && (
            <div className="mt-1 flex gap-3 py-[1px]" aria-hidden="true">
              <span className="w-7 shrink-0 text-[10px] text-ink-muted tabular-nums">
                {String(steps.length + 1).padStart(2, "0")}
              </span>
              <span className="shrink-0 text-[10px] text-accent">›</span>
              <span className="text-[11px] text-accent" style={{ animation: "blink 1s step-end infinite" }}>
                ▌
              </span>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </Panel>
    </div>
  );
}

/**
 * Colour a pipeline step by the agent or phase it mentions.
 *
 * Preserved from the original implementation so the log reads identically
 * across every view.
 */
export function colorizeStep(step: string): string {
  const s = step.toLowerCase();
  if (s.includes("error") || s.includes("fail")) return "var(--red)";
  if (s.includes("apex") || s.includes("routing")) return "var(--green)";
  if (s.includes("scout") || s.includes("search")) return "var(--cyan)";
  if (s.includes("iris") || s.includes("vision")) return "#ff66aa";
  if (s.includes("sigma") || s.includes("threat")) return "var(--amber)";
  if (s.includes("prism") || s.includes("social")) return "#9966ff";
  if (s.includes("nexus") || s.includes("correlat")) return "#ff6644";
  if (s.includes("quill") || s.includes("report")) return "#ccddff";
  if (s.includes("complete") || s.includes("done")) return "var(--green)";
  return "var(--text)";
}
