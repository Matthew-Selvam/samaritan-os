"use client";

/**
 * StatusBar.tsx — the persistent footer: run identity, stream state and the
 * platform stack. Terse by design; this is chrome, not content.
 */

import { API_BASE } from "@/lib/api";
import {
  formatDuration,
  formatRelativeTime,
  statusTone,
  type Tone,
} from "@/lib/format";
import type { Investigation } from "@/lib/types";
import type { StreamStatus } from "@/lib/useInvestigationStream";
import { StatusDot } from "./StatusDot";

export interface StatusBarProps {
  investigation: Investigation | null;
  /** WebSocket lifecycle, when a run is live. */
  streamStatus?: StreamStatus;
  /** Entity count accumulated so far. */
  entityCount?: number;
  /** Signal count accumulated so far. */
  signalCount?: number;
  /** Human label for the active view. */
  viewLabel?: string;
  className?: string;
}

const STREAM_TONE: Record<StreamStatus, Tone> = {
  idle: "idle",
  connecting: "warn",
  open: "ok",
  reconnecting: "warn",
  closed: "idle",
  error: "err",
};

/** Latency of a run in ms, whichever timestamp pair is available. */
function runLatencyMs(inv: Investigation): number | null {
  if (typeof inv.started_at !== "string") return null;
  const start = Date.parse(inv.started_at);
  if (Number.isNaN(start)) return null;
  const end = typeof inv.ended_at === "string" ? Date.parse(inv.ended_at) : Date.now();
  if (Number.isNaN(end)) return null;
  return Math.max(0, end - start);
}

/**
 * Footer chrome.
 *
 * The endpoint is rendered from {@link API_BASE}, which resolves to
 * "same-origin" when unset — so the bar never claims a host the app is not
 * actually talking to.
 */
export function StatusBar({
  investigation,
  streamStatus = "idle",
  entityCount = 0,
  signalCount = 0,
  viewLabel,
  className,
}: StatusBarProps) {
  const latency = investigation ? runLatencyMs(investigation) : null;
  const agents = investigation?.agents_activated?.length ?? 0;

  return (
    <footer
      className={`flex shrink-0 items-center justify-between gap-3 border-t border-border-subtle bg-surface-1 px-3 py-1 ${
        className ?? ""
      }`}
    >
      <div className="flex min-w-0 items-center gap-2 overflow-hidden">
        <span className="mono-label shrink-0 !text-[8px]">SIGNAL-OS</span>
        <span aria-hidden="true" className="text-border-strong">
          │
        </span>
        {viewLabel && <span className="mono-label shrink-0 !text-[8px]">{viewLabel}</span>}
        {investigation ? (
          <>
            <span aria-hidden="true" className="text-border-strong">
              │
            </span>
            <span className="mono-label shrink-0 !text-[8px]">
              ID {investigation.inv_id || "—"}
            </span>
            <StatusDot
              tone={statusTone(investigation.status)}
              dotOnly
              label={`Status ${investigation.status}`}
              pulse={investigation.status === "running" || investigation.status === "routing"}
            />
            {agents > 0 && (
              <span className="mono-label shrink-0 !text-[8px]">
                {agents} AGENT{agents === 1 ? "" : "S"}
              </span>
            )}
            {entityCount > 0 && (
              <span className="mono-label shrink-0 !text-[8px]">{entityCount} ENT</span>
            )}
            {signalCount > 0 && (
              <span className="mono-label shrink-0 !text-[8px]">{signalCount} SIG</span>
            )}
            {latency !== null && (
              <span className="mono-label shrink-0 !text-[8px] tabular-nums">
                {formatDuration(latency)}
              </span>
            )}
          </>
        ) : (
          <span className="mono-label truncate !text-[8px]">NO ACTIVE RUN</span>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        {investigation && streamStatus !== "idle" && (
          <>
            <StatusDot
              tone={STREAM_TONE[streamStatus]}
              dotOnly
              label={`Stream ${streamStatus}`}
              pulse={streamStatus === "open" || streamStatus === "connecting"}
            />
            <span aria-hidden="true" className="text-border-strong">
              │
            </span>
          </>
        )}
        {investigation?.started_at && (
          <span className="mono-label hidden !text-[8px] 2xl:inline">
            {formatRelativeTime(investigation.started_at)}
          </span>
        )}
        <span className="mono-label !text-[8px]">
          {API_BASE ? API_BASE.replace(/^https?:\/\//, "") : "SAME-ORIGIN"}
        </span>
      </div>
    </footer>
  );
}
