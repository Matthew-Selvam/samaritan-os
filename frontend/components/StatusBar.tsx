"use client";
import type { Investigation } from "@/app/page";

interface Props {
  investigation: Investigation | null;
}

export function StatusBar({ investigation }: Props) {
  return (
    <footer
      className="h-6 flex items-center justify-between px-4 border-t flex-shrink-0"
      style={{
        borderColor: "var(--border)",
        background: "var(--bg-panel)",
      }}
    >
      <div className="flex items-center gap-4">
        <span className="label" style={{ fontSize: 8 }}>SIGNAL-OS v0.1.0</span>
        {investigation && (
          <>
            <span style={{ color: "var(--border-hi)" }}>│</span>
            <span className="label" style={{ fontSize: 8 }}>
              ID: {investigation.id || "—"}
            </span>
            <span style={{ color: "var(--border-hi)" }}>│</span>
            <span
              className="label"
              style={{
                fontSize: 8,
                color:
                  investigation.status === "done" ? "var(--green)" :
                  investigation.status === "error" ? "var(--red)" :
                  investigation.status === "running" ? "var(--cyan)" :
                  "var(--amber)",
              }}
            >
              {investigation.status.toUpperCase()}
            </span>
            {investigation.agents_activated.length > 0 && (
              <>
                <span style={{ color: "var(--border-hi)" }}>│</span>
                <span className="label" style={{ fontSize: 8 }}>
                  {investigation.agents_activated.length} agent{investigation.agents_activated.length !== 1 ? "s" : ""} activated
                </span>
              </>
            )}
          </>
        )}
      </div>

      <div className="flex items-center gap-4">
        <span className="label" style={{ fontSize: 8 }}>FastAPI :8766</span>
        <span style={{ color: "var(--border-hi)" }}>│</span>
        <span className="label" style={{ fontSize: 8 }}>LangGraph</span>
        <span style={{ color: "var(--border-hi)" }}>│</span>
        <span className="label" style={{ fontSize: 8 }}>Neo4j · Qdrant · Redis</span>
        <span style={{ color: "var(--border-hi)" }}>│</span>
        <span className="label" style={{ fontSize: 8 }}>14 AGENTS LOADED</span>
      </div>
    </footer>
  );
}
