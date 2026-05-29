"use client";
import { useState, useRef, useCallback } from "react";
import { AgentGrid } from "@/components/AgentGrid";
import { PipelineTrace } from "@/components/PipelineTrace";
import { StatusBar } from "@/components/StatusBar";
import { InputBar } from "@/components/InputBar";
import { TopBar } from "@/components/TopBar";

export type InvestigationStatus = "idle" | "routing" | "running" | "done" | "error";

export interface Investigation {
  id: string;
  input: string;
  input_type: string;
  agents_activated: string[];
  status: InvestigationStatus;
  steps: string[];
  routing_confidence?: number;
  routing_reasoning?: string;
  started_at: number;
  ended_at?: number;
}

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8766";

export default function Home() {
  const [investigation, setInvestigation] = useState<Investigation | null>(null);
  const [activeAgents, setActiveAgents] = useState<Set<string>>(new Set());
  const wsRef = useRef<WebSocket | null>(null);

  const startInvestigation = useCallback(async (input: string) => {
    if (!input.trim()) return;
    wsRef.current?.close();

    const inv: Investigation = {
      id: "",
      input,
      input_type: "unknown",
      agents_activated: [],
      status: "routing",
      steps: [],
      started_at: Date.now(),
    };
    setInvestigation(inv);
    setActiveAgents(new Set());

    try {
      const res = await fetch(`${API}/api/investigate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input }),
      });
      const data = await res.json();
      const newInv = { ...inv, id: data.id, status: "running" as InvestigationStatus };
      setInvestigation(newInv);

      const ws = new WebSocket(`${API.replace(/^http/, "ws")}/ws/pipeline/${data.id}`);
      wsRef.current = ws;

      ws.onmessage = (evt) => {
        const msg = JSON.parse(evt.data);
        if (msg.type === "step") {
          setInvestigation((prev) => prev ? { ...prev, steps: [...prev.steps, msg.message] } : prev);
          const upper = msg.message.toUpperCase();
          setActiveAgents((prev) => {
            const next = new Set(prev);
            ["SCOUT","CRAWLER","PRISM","IRIS","ECHO","TERRA","INK","NEXUS","KRONOS","VAULT","SENTINEL","QUILL","SIGMA","APEX"]
              .forEach((a) => { if (upper.includes(a)) next.add(a); });
            return next;
          });
        } else if (msg.type === "done") {
          setInvestigation((prev) => prev ? { ...prev, status: "done", ended_at: Date.now() } : prev);
          ws.close();
        } else if (msg.type === "error") {
          setInvestigation((prev) => prev ? { ...prev, status: "error", steps: [...prev.steps, `ERROR: ${msg.message}`], ended_at: Date.now() } : prev);
          ws.close();
        }
      };

      // poll REST for routing metadata
      setTimeout(async () => {
        try {
          const r = await fetch(`${API}/api/investigate/${data.id}`);
          const d = await r.json();
          setInvestigation((prev) => prev ? {
            ...prev,
            input_type: d.input_type ?? prev.input_type,
            agents_activated: d.agents_activated ?? prev.agents_activated,
            routing_confidence: d.routing_confidence,
            routing_reasoning: d.routing_reasoning,
          } : prev);
        } catch { /* noop */ }
      }, 800);

    } catch (err) {
      setInvestigation((prev) =>
        prev ? { ...prev, status: "error", steps: [`Connection error: ${err}`], ended_at: Date.now() } : prev
      );
    }
  }, []);

  return (
    <div className="h-screen flex flex-col overflow-hidden" style={{ background: "var(--bg)" }}>
      <TopBar />

      <div className="flex-1 flex overflow-hidden">
        {/* Left sidebar — agent roster */}
        <aside
          className="w-64 flex-shrink-0 overflow-y-auto border-r flex flex-col gap-1 p-3"
          style={{ borderColor: "var(--border)", background: "var(--bg-panel)" }}
        >
          <p className="label mb-2">Intelligence Agents</p>
          <AgentGrid
            activeAgents={activeAgents}
            activatedAgents={new Set(investigation?.agents_activated ?? [])}
          />
        </aside>

        {/* Centre — search + pipeline */}
        <main className="flex-1 flex flex-col overflow-hidden">
          <div className="p-4 border-b" style={{ borderColor: "var(--border)" }}>
            <InputBar
              onSubmit={startInvestigation}
              running={investigation?.status === "routing" || investigation?.status === "running"}
            />
          </div>
          <div className="flex-1 overflow-y-auto p-4">
            {investigation ? (
              <PipelineTrace investigation={investigation} />
            ) : (
              <WelcomeScreen />
            )}
          </div>
        </main>
      </div>

      <StatusBar investigation={investigation} />
    </div>
  );
}

function WelcomeScreen() {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-5 select-none">
      <div
        style={{
          fontSize: 64,
          color: "var(--green)",
          textShadow: "0 0 40px var(--green)",
          lineHeight: 1,
          animation: "spin-slow 20s linear infinite",
        }}
      >
        ⊕
      </div>
      <div className="text-center">
        <p
          className="tracking-[0.3em] font-bold mb-1"
          style={{ fontSize: 18, color: "var(--green)" }}
        >
          SIGNAL-OS
        </p>
        <p className="label mb-4">INPUT ANYTHING · EXTRACT EVERYTHING</p>
        <p style={{ color: "var(--text-muted)", fontSize: 11 }}>
          username &nbsp;·&nbsp; email &nbsp;·&nbsp; phone &nbsp;·&nbsp; domain &nbsp;·&nbsp; IP &nbsp;·&nbsp; wallet<br />
          URL &nbsp;·&nbsp; image &nbsp;·&nbsp; video &nbsp;·&nbsp; audio &nbsp;·&nbsp; document
        </p>
      </div>
    </div>
  );
}
