"use client";

/**
 * OpsecIndicator.tsx — circuit state chip plus rotation control.
 *
 * Talks to `lib/api.ts` (`getOpsecStatus` / `newCircuit`) so the endpoint is
 * never hardcoded, and normalises both the typed `OpsecStatus` and the legacy
 * `{connected, mode, exit_country}` shape the older backend returns.
 */

import { useCallback, useEffect, useState } from "react";
import { getOpsecStatus, newCircuit } from "@/lib/api";
import { formatRelativeTime } from "@/lib/format";
import type { OpsecStatus } from "@/lib/types";
import { Badge, Button, DefRow, Modal } from "./ui";
import { StatusDot } from "./StatusDot";
import type { Tone } from "@/lib/format";

/** Normalised view model over both backend generations. */
export interface OpsecView {
  active: boolean;
  mode: "tor" | "direct" | "unknown";
  label: string;
  detail?: string;
  exitCountry?: string;
  hops?: number;
  coverage?: number;
  startedAt?: string;
  expiresAt?: string;
  circuitId?: string;
}

const TONE_BY_MODE: Record<OpsecView["mode"], Tone> = {
  tor: "ok",
  direct: "err",
  unknown: "idle",
};

const LABEL_BY_MODE: Record<OpsecView["mode"], string> = {
  tor: "TOR",
  direct: "DIRECT",
  unknown: "UNKNOWN",
};

/**
 * Fold either `OpsecStatus` shape into {@link OpsecView}.
 * Never throws — an unreadable payload becomes `unknown` rather than a crash.
 */
export function toOpsecView(raw: unknown): OpsecView {
  if (!raw || typeof raw !== "object") {
    return { active: false, mode: "unknown", label: "UNKNOWN" };
  }
  const rec = raw as Record<string, unknown>;
  const circuit =
    rec.circuit && typeof rec.circuit === "object"
      ? (rec.circuit as Record<string, unknown>)
      : null;

  // Legacy shape: { connected, mode, exit_country, exit_ip }
  if (typeof rec.connected === "boolean" && typeof rec.mode === "string") {
    const legacyMode = rec.mode === "tor" ? "tor" : rec.mode === "direct" ? "direct" : "unknown";
    return {
      active: rec.connected && legacyMode === "tor",
      mode: legacyMode,
      label: LABEL_BY_MODE[legacyMode],
      exitCountry: typeof rec.exit_country === "string" ? rec.exit_country : undefined,
      detail: typeof rec.exit_ip === "string" ? rec.exit_ip : undefined,
    };
  }

  const status = rec as unknown as OpsecStatus;
  const active = status.active === true;
  const hops = status.hops ?? status.circuit?.hops;
  const coverage = status.coverage;
  const mode: OpsecView["mode"] = active ? "tor" : status.circuit_id ? "unknown" : "direct";
  return {
    active,
    mode,
    label: active ? "TOR" : status.circuit_id ? "CIRCUIT" : LABEL_BY_MODE.direct,
    detail: status.circuit_label ?? status.circuit?.label ?? undefined,
    hops,
    coverage,
    startedAt: status.started_at ?? status.circuit?.created_at,
    expiresAt: status.expires_at ?? status.circuit?.expires_at,
    circuitId: status.circuit_id ?? status.circuit?.id,
  };
}

export interface OpsecIndicatorProps {
  /** Poll interval in ms. */
  pollMs?: number;
  /** Shows the circuit-rotation confirmation dialog first. */
  confirmRotate?: boolean;
  /** Notified after a successful rotation. */
  onRotated?: (view: OpsecView) => void;
  /** Notified when the backend reports a failure. */
  onError?: (message: string) => void;
  className?: string;
}

/**
 * Live opsec chip.
 *
 * Failure is explicit: a poll error marks the chip `unknown` and offers a
 * retry rather than silently showing "direct" and implying traffic is exposed.
 */
export function OpsecIndicator({
  pollMs = 15_000,
  confirmRotate = true,
  onRotated,
  onError,
  className,
}: OpsecIndicatorProps) {
  const [view, setView] = useState<OpsecView>({ active: false, mode: "unknown", label: "CHECKING" });
  const [detailOpen, setDetailOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [rotating, setRotating] = useState(false);
  const [pollError, setPollError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const status = await getOpsecStatus({ timeout_ms: 10_000 });
      setView(toOpsecView(status));
      setPollError(null);
    } catch (err) {
      setView((prev) => ({ ...prev, mode: "unknown", label: "UNKNOWN" }));
      setPollError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
    if (pollMs <= 0) return;
    const id = window.setInterval(() => void load(), pollMs);
    return () => window.clearInterval(id);
  }, [load, pollMs]);

  const rotate = useCallback(async () => {
    setRotating(true);
    try {
      const next = await newCircuit({ timeout_ms: 30_000 });
      const nextView = toOpsecView(next);
      setView(nextView);
      setPollError(null);
      onRotated?.(nextView);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setPollError(message);
      onError?.(message);
    } finally {
      setRotating(false);
      setConfirmOpen(false);
      void load();
    }
  }, [load, onError, onRotated]);

  const tone = TONE_BY_MODE[view.mode];
  const title = pollError
    ? `Opsec status unavailable: ${pollError}`
    : `Traffic ${view.active ? "is" : "is not"} routed through the active circuit`;

  return (
    <div className={className}>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => setDetailOpen(true)}
          title={title}
          aria-label={`Opsec ${view.label}. Open details`}
          className="focus-ring flex items-center gap-1.5 rounded px-1 py-0.5"
        >
          <svg
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke={view.active ? "var(--green)" : view.mode === "direct" ? "var(--red)" : "var(--amber)"}
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
          <StatusDot
            tone={tone}
            dotOnly
            pulse={view.mode === "unknown"}
            label={`Opsec ${view.label}`}
          />
          <span className="mono-label !text-[9px]">{view.label}</span>
          {view.exitCountry && (
            <span className="mono-label !text-[8px]">[{view.exitCountry}]</span>
          )}
        </button>

        <Button
          size="sm"
          variant="ghost"
          loading={rotating}
          onClick={() => (confirmRotate ? setConfirmOpen(true) : void rotate())}
          title="Rotate to a fresh circuit"
        >
          Rotate
        </Button>
      </div>

      <Modal
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        title="OPSEC circuit"
        width={420}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => void load()}>
              Refresh
            </Button>
            <Button size="sm" variant="solid" onClick={() => setConfirmOpen(true)}>
              Rotate circuit
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-2 p-3">
          {pollError && (
            <p role="alert" className="m-0 break-words text-[10px] text-signal-err">
              {pollError}
            </p>
          )}
          <div>
            <DefRow label="State">
              <Badge tone={tone}>{view.label}</Badge>
            </DefRow>
            <DefRow label="Routed">{view.active ? "YES" : "NO"}</DefRow>
            {view.circuitId && <DefRow label="Circuit" mono>{view.circuitId}</DefRow>}
            {view.detail && <DefRow label="Label">{view.detail}</DefRow>}
            {view.hops !== undefined && <DefRow label="Hops">{view.hops}</DefRow>}
            {view.coverage !== undefined && (
              <DefRow label="Coverage">{Math.round(view.coverage * 100)}%</DefRow>
            )}
            {view.startedAt && (
              <DefRow label="Started">{formatRelativeTime(view.startedAt)}</DefRow>
            )}
            {view.expiresAt && (
              <DefRow label="Expires">{formatRelativeTime(view.expiresAt)}</DefRow>
            )}
          </div>
        </div>
      </Modal>

      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title="Rotate circuit?"
        width={380}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" variant="solid" loading={rotating} onClick={() => void rotate()}>
              Rotate
            </Button>
          </>
        }
      >
        <p className="m-0 p-3 text-[11px] leading-relaxed text-ink-muted">
          This requests a new exit node and a new identity. In-flight requests routed through the
          old circuit may fail; connectors that cache the exit should be re-checked afterwards.
        </p>
      </Modal>
    </div>
  );
}
