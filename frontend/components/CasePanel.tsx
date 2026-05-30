"use client";
import { useEffect, useState } from "react";
import type { Investigation } from "@/app/page";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8766";

const INPUT_TYPE_COLORS: Record<string, string> = {
  email:         "#00d4ff",
  domain:        "#00ff88",
  ip_address:    "#ffb020",
  username:      "#9966ff",
  crypto_wallet: "#ff8833",
  url:           "#44dd88",
  image:         "#ff66aa",
  video:         "#ff66aa",
  audio:         "#ffb020",
  document:      "#aabbff",
  phone:         "#ffcc44",
  text:          "#c8d8e8",
  unknown:       "#5a7a9a",
};

const STATUS_COLOR: Record<Investigation["status"], string> = {
  idle:    "var(--text-muted)",
  routing: "var(--amber)",
  running: "var(--cyan)",
  done:    "var(--green)",
  error:   "var(--red)",
};

function elapsedLabel(inv: Investigation): string {
  const end  = inv.ended_at ?? Date.now();
  const secs = Math.round((end - inv.started_at) / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const rem  = secs % 60;
  return `${mins}m ${rem}s`;
}

interface Props {
  onSelect: (investigation: Investigation) => void;
  activeId?: string;
}

export function CasePanel({ onSelect, activeId }: Props) {
  const [cases, setCases] = useState<Investigation[]>([]);

  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetch(`${API}/api/investigations`);
        if (!res.ok) return;
        const data: Investigation[] = await res.json();
        setCases(data.slice().reverse()); // newest first
      } catch { /* backend not ready */ }
    };

    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", gap: 0 }}>
      {/* Header */}
      <div
        style={{
          padding: "10px 12px 8px",
          borderBottom: "1px solid var(--border)",
          flexShrink: 0,
        }}
      >
        <p
          className="label"
          style={{ fontSize: 9, letterSpacing: "0.12em", color: "var(--text-muted)" }}
        >
          PAST INVESTIGATIONS
        </p>
        {cases.length > 0 && (
          <p style={{ fontSize: 9, color: "var(--text-muted)", marginTop: 2, opacity: 0.55 }}>
            {cases.length} case{cases.length !== 1 ? "s" : ""}
          </p>
        )}
      </div>

      {/* Case list */}
      <div style={{ flex: 1, overflowY: "auto", padding: "8px 8px" }}>
        {cases.length === 0 ? (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              height: 120,
              gap: 6,
              pointerEvents: "none",
            }}
          >
            <div style={{ fontSize: 22, color: "var(--border-hi)", lineHeight: 1 }}>◌</div>
            <p style={{ fontSize: 10, color: "var(--text-muted)", textAlign: "center" }}>
              No investigations yet
            </p>
          </div>
        ) : (
          cases.map((inv) => (
            <CaseCard
              key={inv.id}
              inv={inv}
              isActive={inv.id === activeId}
              onSelect={onSelect}
            />
          ))
        )}
      </div>
    </div>
  );
}

// ── Individual case card ──────────────────────────────────────────────────────

interface CardProps {
  inv: Investigation;
  isActive: boolean;
  onSelect: (inv: Investigation) => void;
}

function CaseCard({ inv, isActive, onSelect }: CardProps) {
  const typeColor   = INPUT_TYPE_COLORS[inv.input_type] ?? "var(--text-muted)";
  const statusColor = STATUS_COLOR[inv.status];
  const isLive      = inv.status === "running" || inv.status === "routing";

  return (
    <div
      onClick={() => onSelect(inv)}
      style={{
        marginBottom: 6,
        padding:      "8px 10px",
        borderRadius: 5,
        background:   isActive ? "rgba(0,255,136,0.06)" : "var(--bg-card)",
        border:       `1px solid ${isActive ? "var(--border-hi)" : "var(--border)"}`,
        cursor:       "pointer",
        transition:   "border-color 0.12s, background 0.12s",
        position:     "relative",
      }}
      onMouseEnter={(e) => {
        if (!isActive) {
          e.currentTarget.style.borderColor = "var(--border-hi)";
          e.currentTarget.style.background  = "rgba(255,255,255,0.03)";
        }
      }}
      onMouseLeave={(e) => {
        if (!isActive) {
          e.currentTarget.style.borderColor = "var(--border)";
          e.currentTarget.style.background  = "var(--bg-card)";
        }
      }}
    >
      {/* Input preview — truncated */}
      <p
        style={{
          fontSize:     11,
          color:        "var(--text)",
          overflow:     "hidden",
          whiteSpace:   "nowrap",
          textOverflow: "ellipsis",
          marginBottom: 5,
          paddingRight: 10,
          fontFamily:   "var(--font-mono)",
        }}
      >
        {inv.input}
      </p>

      {/* Meta row */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        {/* input_type badge */}
        <span
          style={{
            fontSize:      8,
            fontWeight:    700,
            letterSpacing: "0.1em",
            textTransform: "uppercase",
            padding:       "1px 5px",
            borderRadius:  3,
            background:    `${typeColor}18`,
            border:        `1px solid ${typeColor}44`,
            color:         typeColor,
            flexShrink:    0,
          }}
        >
          {inv.input_type}
        </span>

        {/* Status dot + label */}
        <div style={{ display: "flex", alignItems: "center", gap: 4, flexShrink: 0 }}>
          <span
            style={{
              width:        5,
              height:       5,
              borderRadius: "50%",
              background:   statusColor,
              display:      "inline-block",
              boxShadow:    `0 0 4px ${statusColor}`,
              animation:    isLive ? "pulse-green 2s ease-in-out infinite" : undefined,
              flexShrink:   0,
            }}
          />
          <span
            style={{
              fontSize:      8,
              color:         statusColor,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
            }}
          >
            {inv.status}
          </span>
        </div>

        {/* Spacer */}
        <span style={{ flex: 1 }} />

        {/* Elapsed */}
        <span
          style={{
            fontSize:           9,
            color:              "var(--text-muted)",
            fontVariantNumeric: "tabular-nums",
            flexShrink:         0,
          }}
        >
          {elapsedLabel(inv)}
        </span>
      </div>

      {/* Active indicator — left edge bar */}
      {isActive && (
        <div
          style={{
            position:     "absolute",
            left:         0,
            top:          4,
            bottom:       4,
            width:        2,
            borderRadius: "0 2px 2px 0",
            background:   "var(--green)",
            boxShadow:    "0 0 6px var(--green)",
          }}
        />
      )}
    </div>
  );
}
