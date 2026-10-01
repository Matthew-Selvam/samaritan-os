"use client";

/**
 * CasePanel.tsx — the recent-investigations rail.
 *
 * Data now comes from `lib/api.ts` (`listInvestigations`) instead of a raw
 * `fetch` against a hardcoded host, and the row/card markup is built from the
 * shared `Panel`/`Badge` primitives. Keyboard support is real: each row is a
 * button, the list is announced as a listbox and arrow keys move the selection.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { listInvestigations } from "@/lib/api";
import {
  formatDuration,
  formatRelativeTime,
  statusLabel,
  statusTone,
  truncate,
  type Tone,
} from "@/lib/format";
import type { Investigation, InvestigationStatus } from "@/lib/types";
import { Badge, Button, EmptyState, ErrorState, Panel, SkeletonRows } from "./ui";
import { apiCall } from "@/lib/hooks";
import { inputTypeColor } from "./PipelineTrace";

export interface CasePanelProps {
  onSelect: (investigation: Investigation) => void;
  /** Inv id of the run currently on screen. */
  activeId?: string | null;
  /** Restrict the list to one case. */
  caseId?: string | null;
  /** How many rows to keep in the DOM. */
  limit?: number;
  /** Poll interval in ms; `0` disables polling. */
  pollMs?: number;
  className?: string;
  title?: string;
}

const STATUS_TONE_FALLBACK: Record<string, Tone> = {
  queued: "warn",
  pending: "warn",
};

/** Tone for any status string the backend might emit. */
function toneFor(investigation: Investigation): Tone {
  const status = investigation.status as InvestigationStatus;
  return statusTone(status) === "idle" && STATUS_TONE_FALLBACK[status]
    ? STATUS_TONE_FALLBACK[status]
    : statusTone(status);
}

/** Duration between two ISO timestamps, in ms. */
function elapsedLabel(inv: Investigation): string {
  if (typeof inv.started_at !== "string") return "—";
  const start = Date.parse(inv.started_at);
  if (Number.isNaN(start)) return "—";
  const end =
    typeof inv.ended_at === "string" ? Date.parse(inv.ended_at) : Date.now();
  if (Number.isNaN(end)) return "—";
  return formatDuration(Math.max(0, end - start));
}

/**
 * Recent investigations, newest first.
 *
 * A failed fetch renders an inline error with retry — never a silent blank
 * rail — and a live run keeps polling so its status stays current.
 */
export function CasePanel({
  onSelect,
  activeId = null,
  caseId = null,
  limit = 40,
  pollMs = 5000,
  className,
  title = "Recent investigations",
}: CasePanelProps) {
  const [rows, setRows] = useState<Investigation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    apiCall(
      () =>
        listInvestigations(
          { case_id: caseId ?? undefined, limit },
          { signal: controller.signal, timeout_ms: 15_000 },
        ),
      `/api/investigations${caseId ? `?case_id=${encodeURIComponent(caseId)}` : ""}`,
    )
      .then((items) => {
        if (cancelled) return;
        setRows(items);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled || controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [caseId, limit, nonce]);

  useEffect(() => {
    if (pollMs <= 0) return;
    const id = window.setInterval(() => setNonce((n: number) => n + 1), pollMs);
    return () => window.clearInterval(id);
  }, [pollMs]);

  const refresh = useCallback(() => setNonce((n: number) => n + 1), []);

  const ordered = useMemo(
    () => rows.slice().sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? "")),
    [rows],
  );

  const onKeyDown = (event: React.KeyboardEvent<HTMLUListElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const buttons = Array.from(
      listRef.current?.querySelectorAll<HTMLButtonElement>("button[data-row]") ?? [],
    );
    if (buttons.length === 0) return;
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === "ArrowDown" ? index + 1 : index - 1;
    const wrapped = (next + buttons.length) % buttons.length;
    buttons[wrapped]?.focus();
  };

  return (
    <Panel
      title={title}
      eyebrow="HISTORY"
      actions={
        <>
          <Badge tone="idle">{ordered.length}</Badge>
          <Button
            size="sm"
            variant="ghost"
            iconLabel="Refresh investigation history"
            onClick={refresh}
          >
            ⟳
          </Button>
        </>
      }
      bodyClassName="!p-2"
      className={className}
      aria-label={title}
    >
      {loading && ordered.length === 0 ? (
        <SkeletonRows rows={4} height={44} />
      ) : error && ordered.length === 0 ? (
        <ErrorState
          compact
          message={
            caseId
              ? `${error}\n\nThe rail is filtered to case ${caseId}; this backend answered a different route.`
              : error
          }
          onRetry={refresh}
          title="Could not load history"
        />
      ) : ordered.length === 0 ? (
        <EmptyState
          compact
          glyph="◌"
          title="NO INVESTIGATIONS YET"
          description="Run a target from the input bar; completed runs appear here."
        />
      ) : (
        <ul
          ref={listRef}
          aria-label="Recent investigations"
          onKeyDown={onKeyDown}
          className="scroll-thin flex max-h-full flex-col gap-1.5 overflow-y-auto"
        >
          {ordered.map((inv) => {
            const active = Boolean(activeId) && inv.inv_id === activeId;
            const live = inv.status === "running" || inv.status === "routing";
            const tone = toneFor(inv);
            const typeColor = inputTypeColor(inv.input_type);
            return (
              <li key={inv.inv_id} className="min-w-0">
                <button
                  type="button"
                  data-row
                  onClick={() => onSelect(inv)}
                  aria-current={active ? "true" : undefined}
                  title={inv.input}
                  className="focus-ring relative w-full min-w-0 rounded border px-2 py-1.5 text-left transition-colors"
                  style={{
                    background: active ? "var(--tint-accent-06)" : "var(--bg-card)",
                    borderColor: active ? "var(--border-hi)" : "var(--border)",
                  }}
                >
                  <span
                    className="block truncate text-[11px] text-ink"
                    style={{ paddingRight: 6 }}
                  >
                    {truncate(inv.input, 44)}
                  </span>
                  <span className="mt-1 flex flex-wrap items-center gap-1.5">
                    <Badge color={typeColor}>{inv.input_type || "unknown"}</Badge>
                    <Badge tone={tone} pulse={live}>
                      {statusLabel(inv.status)}
                    </Badge>
                    <span className="mono-label ml-auto !text-[8px] tabular-nums">
                      {elapsedLabel(inv)}
                    </span>
                  </span>
                  <span className="mono-label mt-1 block !text-[8px]">
                    {inv.started_at ? formatRelativeTime(inv.started_at) : "—"}
                    {inv.case_id ? ` · ${inv.case_id}` : ""}
                  </span>
                  {active && (
                    <span
                      aria-hidden="true"
                      className="absolute bottom-1 left-0 top-1 w-[2px] rounded-r bg-accent"
                      style={{ boxShadow: "0 0 6px var(--green)" }}
                    />
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
