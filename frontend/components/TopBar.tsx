"use client";
import { useState, useEffect } from "react";

const AGENT_COUNT = 14;
const BACKEND = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8766";

export function TopBar() {
  const [connected, setConnected] = useState(false);
  const [time, setTime] = useState("");

  useEffect(() => {
    const tick = () => setTime(new Date().toISOString().replace("T", " ").slice(0, 19) + " UTC");
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const check = async () => {
      try {
        await fetch(`${BACKEND}/api/health`);
        setConnected(true);
      } catch {
        setConnected(false);
      }
    };
    check();
    const id = setInterval(check, 10000);
    return () => clearInterval(id);
  }, []);

  return (
    <header
      className="flex items-center justify-between px-4 h-10 border-b flex-shrink-0"
      style={{ borderColor: "var(--border)", background: "var(--bg-panel)" }}
    >
      {/* Left — brand */}
      <div className="flex items-center gap-3">
        <span
          style={{ color: "var(--green)", fontSize: 16, textShadow: "0 0 8px var(--green)" }}
        >
          ⊕
        </span>
        <span
          className="tracking-[0.25em] font-bold"
          style={{ color: "var(--green)", fontSize: 12 }}
        >
          SIGNAL-OS
        </span>
        <span className="label" style={{ fontSize: 9 }}>
          v0.1.0-alpha
        </span>
      </div>

      {/* Centre — nav tabs */}
      <nav className="flex gap-1">
        {["INVESTIGATE", "GRAPH", "TIMELINE", "REPORTS", "MONITOR"].map((tab) => (
          <button
            key={tab}
            className="px-3 py-1 rounded text-xs tracking-widest transition-colors"
            style={{
              color: tab === "INVESTIGATE" ? "var(--green)" : "var(--text-muted)",
              background: tab === "INVESTIGATE" ? "rgba(0,255,136,0.08)" : "transparent",
              border: `1px solid ${tab === "INVESTIGATE" ? "var(--border-hi)" : "transparent"}`,
              cursor: "pointer",
            }}
          >
            {tab}
          </button>
        ))}
      </nav>

      {/* Right — system info */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5">
          <span
            className="rounded-full"
            style={{
              width: 6, height: 6,
              background: connected ? "var(--green)" : "var(--red)",
              boxShadow: connected ? "0 0 6px var(--green)" : "0 0 6px var(--red)",
              display: "inline-block",
              animation: connected ? "pulse-green 2s ease-in-out infinite" : undefined,
            }}
          />
          <span className="label" style={{ fontSize: 9 }}>
            {connected ? "BACKEND ONLINE" : "BACKEND OFFLINE"}
          </span>
        </div>
        <span className="label" style={{ fontSize: 9, color: "var(--text-muted)" }}>
          {AGENT_COUNT} AGENTS
        </span>
        <span className="label" style={{ fontSize: 9, fontVariantNumeric: "tabular-nums" }}>
          {time}
        </span>
      </div>
    </header>
  );
}
