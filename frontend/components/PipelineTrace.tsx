"use client";
import { useEffect, useRef } from "react";
import type { Investigation } from "@/app/page";

const INPUT_TYPE_COLORS: Record<string, string> = {
  email:        "#00d4ff",
  domain:       "#00ff88",
  ip_address:   "#ffb020",
  username:     "#9966ff",
  crypto_wallet:"#ff8833",
  url:          "#44dd88",
  image:        "#ff66aa",
  video:        "#ff66aa",
  audio:        "#ffb020",
  document:     "#aabbff",
  phone:        "#ffcc44",
  text:         "#c8d8e8",
  unknown:      "#5a7a9a",
};

interface Props {
  investigation: Investigation;
}

export function PipelineTrace({ investigation }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [investigation.steps]);

  const typeColor = INPUT_TYPE_COLORS[investigation.input_type] ?? "var(--text-muted)";
  const elapsed = investigation.ended_at
    ? ((investigation.ended_at - investigation.started_at) / 1000).toFixed(2)
    : ((Date.now() - investigation.started_at) / 1000).toFixed(1);

  return (
    <div className="flex flex-col gap-4" style={{ animation: "slide-in 0.2s ease-out" }}>
      {/* Header card */}
      <div
        className="rounded-md p-3"
        style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}
      >
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <p className="label mb-1">Target Input</p>
            <p style={{ color: "var(--text)", fontSize: 14, wordBreak: "break-all" }}>
              {investigation.input}
            </p>
          </div>
          <div className="flex gap-6 flex-shrink-0">
            {/* Input type badge */}
            <div>
              <p className="label mb-1">Type</p>
              <span
                className="px-2 py-0.5 rounded text-xs font-bold tracking-widest uppercase"
                style={{
                  background: `${typeColor}18`,
                  border: `1px solid ${typeColor}44`,
                  color: typeColor,
                }}
              >
                {investigation.input_type}
              </span>
            </div>

            {/* Status */}
            <div>
              <p className="label mb-1">Status</p>
              <StatusBadge status={investigation.status} />
            </div>

            {/* Elapsed */}
            <div>
              <p className="label mb-1">Elapsed</p>
              <span style={{ color: "var(--text)", fontSize: 12, fontVariantNumeric: "tabular-nums" }}>
                {elapsed}s
              </span>
            </div>

            {/* Confidence */}
            {investigation.routing_confidence !== undefined && (
              <div>
                <p className="label mb-1">Confidence</p>
                <span style={{ color: "var(--green)", fontSize: 12 }}>
                  {Math.round(investigation.routing_confidence * 100)}%
                </span>
              </div>
            )}
          </div>
        </div>

        {/* Agents activated */}
        {investigation.agents_activated.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            <span className="label mr-1">Agents:</span>
            {investigation.agents_activated.map((a) => (
              <span
                key={a}
                className="px-2 py-0.5 rounded text-xs"
                style={{
                  background: "rgba(0,255,136,0.1)",
                  border: "1px solid rgba(0,255,136,0.25)",
                  color: "var(--green)",
                  letterSpacing: "0.08em",
                }}
              >
                {a}
              </span>
            ))}
          </div>
        )}

        {/* Routing reasoning */}
        {investigation.routing_reasoning && (
          <p className="mt-2 label" style={{ fontSize: 10, color: "var(--text-muted)" }}>
            {investigation.routing_reasoning}
          </p>
        )}
      </div>

      {/* Pipeline trace */}
      <div
        className="rounded-md p-3 font-mono"
        style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}
      >
        <p className="label mb-3">Pipeline Trace</p>
        {investigation.steps.map((step, i) => (
          <div
            key={i}
            className="flex gap-3 py-0.5"
            style={{ animation: "slide-in 0.15s ease-out" }}
          >
            <span style={{ color: "var(--text-muted)", fontSize: 10, minWidth: 28 }}>
              {String(i + 1).padStart(2, "0")}
            </span>
            <span style={{ color: "var(--green)", fontSize: 10 }}>›</span>
            <span style={{ fontSize: 11, color: colorizeStep(step) }}>
              {step}
            </span>
          </div>
        ))}
        {(investigation.status === "routing" || investigation.status === "running") && (
          <div className="flex gap-3 py-0.5 mt-1">
            <span style={{ color: "var(--text-muted)", fontSize: 10, minWidth: 28 }}>
              {String(investigation.steps.length + 1).padStart(2, "0")}
            </span>
            <span style={{ color: "var(--green)", fontSize: 10 }}>›</span>
            <span
              style={{
                fontSize: 11,
                color: "var(--green)",
                animation: "blink 1s step-end infinite",
              }}
            >
              ▌
            </span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: Investigation["status"] }) {
  const cfg = {
    idle:    { color: "var(--text-muted)", label: "IDLE" },
    routing: { color: "var(--amber)", label: "ROUTING" },
    running: { color: "var(--cyan)", label: "RUNNING" },
    done:    { color: "var(--green)", label: "COMPLETE" },
    error:   { color: "var(--red)", label: "ERROR" },
  }[status];

  return (
    <span
      className="px-2 py-0.5 rounded text-xs font-bold tracking-widest"
      style={{
        background: `${cfg.color}18`,
        border: `1px solid ${cfg.color}44`,
        color: cfg.color,
        animation: status === "running" ? "pulse-green 2s ease-in-out infinite" : undefined,
      }}
    >
      {cfg.label}
    </span>
  );
}

function colorizeStep(step: string): string {
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
