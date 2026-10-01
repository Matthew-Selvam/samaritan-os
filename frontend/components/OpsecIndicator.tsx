"use client";
import { useState, useEffect, useRef, useCallback } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8766";

interface OpsecStatus {
  connected: boolean;
  mode: "tor" | "direct" | "checking";
  exit_country?: string;
  exit_ip?: string;
  request_count?: number;
}

type StatusLevel = "tor" | "direct" | "checking";

const STATUS_META: Record<StatusLevel, { color: string; label: string; glow: string }> = {
  tor:      { color: "var(--green)", label: "TOR",      glow: "0 0 6px var(--green)" },
  direct:   { color: "var(--red)",   label: "DIRECT",   glow: "0 0 6px var(--red)" },
  checking: { color: "var(--amber)", label: "CHECKING", glow: "0 0 6px var(--amber)" },
};

export function OpsecIndicator() {
  const [status, setStatus] = useState<OpsecStatus>({
    connected: false,
    mode: "checking",
  });
  const [hovered, setHovered] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  /* ── poll opsec status ── */
  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/opsec/status`);
      if (res.ok) {
        const data: OpsecStatus = await res.json();
        setStatus(data);
      } else {
        setStatus({ connected: false, mode: "direct" });
      }
    } catch {
      setStatus({ connected: false, mode: "direct" });
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, 15000);
    return () => clearInterval(id);
  }, [fetchStatus]);

  /* ── new circuit ── */
  const newCircuit = async () => {
    setRequesting(true);
    try {
      await fetch(`${API}/api/opsec/new-circuit`, { method: "POST" });
      await fetchStatus();
    } catch {
      /* swallow */
    } finally {
      setRequesting(false);
    }
  };

  const meta = STATUS_META[status.mode] ?? STATUS_META.checking;

  return (
    <div
      ref={containerRef}
      className="relative flex items-center gap-1.5"
      style={{ height: 24, cursor: "default" }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {/* shield icon */}
      <svg
        width="12"
        height="12"
        viewBox="0 0 24 24"
        fill="none"
        stroke={meta.color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ flexShrink: 0 }}
      >
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
      </svg>

      {/* status dot */}
      <span
        style={{
          width: 5,
          height: 5,
          borderRadius: "50%",
          background: meta.color,
          boxShadow: meta.glow,
          display: "inline-block",
          animation: status.mode === "checking" ? "pulse-amber 1.5s ease-in-out infinite" : undefined,
        }}
      />

      {/* label */}
      <span
        className="label"
        style={{
          fontSize: 9,
          color: meta.color,
          fontWeight: 700,
          letterSpacing: "0.1em",
        }}
      >
        {meta.label}
      </span>

      {/* exit country */}
      {status.mode === "tor" && status.exit_country && (
        <span
          className="label"
          style={{
            fontSize: 8,
            color: "var(--text-muted)",
            marginLeft: 2,
          }}
        >
          [{status.exit_country}]
        </span>
      )}

      {/* ── hover tooltip ── */}
      {hovered && (
        <div
          style={{
            position: "absolute",
            top: "calc(100% + 6px)",
            right: 0,
            zIndex: 100,
            background: "var(--bg-panel)",
            border: "1px solid var(--border-hi)",
            borderRadius: 6,
            padding: "8px 12px",
            minWidth: 180,
            boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
          }}
        >
          <div className="flex flex-col gap-1.5">
            {/* exit IP */}
            <div className="flex items-center justify-between gap-4">
              <span style={{ color: "var(--text-muted)", fontSize: 9, letterSpacing: "0.1em" }}>
                EXIT IP
              </span>
              <span
                style={{
                  color: "var(--text)",
                  fontSize: 10,
                  fontFamily: "var(--font-mono)",
                }}
              >
                {status.exit_ip ?? "—"}
              </span>
            </div>

            {/* request count */}
            <div className="flex items-center justify-between gap-4">
              <span style={{ color: "var(--text-muted)", fontSize: 9, letterSpacing: "0.1em" }}>
                REQUESTS
              </span>
              <span
                style={{
                  color: "var(--cyan)",
                  fontSize: 10,
                  fontFamily: "var(--font-mono)",
                }}
              >
                {status.request_count ?? 0}
              </span>
            </div>

            {/* divider */}
            <div style={{ height: 1, background: "var(--border)", margin: "4px 0" }} />

            {/* new circuit button */}
            <button
              onClick={(e) => {
                e.stopPropagation();
                newCircuit();
              }}
              disabled={requesting}
              className="flex items-center justify-center gap-1.5 py-1 rounded"
              style={{
                background: requesting
                  ? "rgba(0,212,255,0.04)"
                  : "rgba(0,212,255,0.08)",
                border: "1px solid rgba(0,212,255,0.2)",
                color: "var(--cyan)",
                fontSize: 9,
                fontWeight: 700,
                letterSpacing: "0.15em",
                cursor: requesting ? "wait" : "pointer",
                transition: "background 0.2s ease",
                opacity: requesting ? 0.5 : 1,
              }}
              onMouseEnter={(e) => {
                if (!requesting)
                  e.currentTarget.style.background = "rgba(0,212,255,0.15)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "rgba(0,212,255,0.08)";
              }}
            >
              <svg
                width="10"
                height="10"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
                style={{
                  animation: requesting ? "spin 1s linear infinite" : undefined,
                }}
              >
                <polyline points="23 4 23 10 17 10" />
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
              </svg>
              {requesting ? "ROTATING..." : "NEW CIRCUIT"}
            </button>
          </div>
        </div>
      )}

      {/* keyframe for checking pulse */}
      <style jsx>{`
        @keyframes pulse-amber {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
        @keyframes spin {
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
