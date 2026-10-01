"use client";

/**
 * AgentGrid.tsx — the live agent roster.
 *
 * Agent identity (name, role, icon, model, routing) comes from
 * `lib/agentCatalog.ts`; live status comes from the pipeline. When the catalog
 * has not loaded (or the app is running against an older backend) the local
 * `FALLBACK_AGENTS` table keeps the grid populated rather than empty.
 */

import { useMemo } from "react";
import clsx from "clsx";
import { Badge } from "./ui";
import { StatusDot } from "./StatusDot";
import { agentTone, formatSeconds, type Tone } from "@/lib/format";
import type { AgentMeta, AgentResult, AgentStatus } from "@/lib/types";
import {
  AGENT_CATALOG,
  type AgentCatalogEntry,
} from "@/lib/agentCatalog";

/**
 * Minimal roster used when `AGENT_CATALOG` is empty or the import failed at
 * runtime. Mirrors `backend/agents/*.py` (name / role / icon).
 */
const FALLBACK_AGENTS: readonly AgentCatalogEntry[] = [
  { name: "APEX", role: "Master Supervisor", icon: "⊕", tier: "supervisor", description: "Routes investigations and orchestrates the swarm." },
  { name: "SCOUT", role: "Search Intelligence", icon: "◎", tier: "primary", description: "Dorks, federation, query expansion." },
  { name: "CRAWLER", role: "Web Scraper", icon: "⟨/⟩", tier: "primary", description: "Structured extraction, browser automation." },
  { name: "PRISM", role: "Social Intelligence", icon: "◈", tier: "primary", description: "Cross-platform identity resolution." },
  { name: "IRIS", role: "Vision Intelligence", icon: "◉", tier: "primary", description: "Face clustering, object detection, OCR." },
  { name: "ECHO", role: "Audio Intelligence", icon: "~", tier: "primary", description: "Transcription, accent/environment analysis." },
  { name: "TERRA", role: "GEOINT", icon: "⊛", tier: "primary", description: "Geolocation, environmental inference." },
  { name: "INK", role: "Stylometry", icon: "✦", tier: "primary", description: "Writing fingerprinting, authorship." },
  { name: "NEXUS", role: "Correlation Engine", icon: "∞", tier: "correlation", description: "Hidden relationship detection." },
  { name: "KRONOS", role: "Timeline Reconstruction", icon: "⊕", tier: "correlation", description: "Chronology reconstruction." },
  { name: "VAULT", role: "Memory Agent", icon: "□", tier: "correlation", description: "Persistent entity memory, semantic summarisation." },
  { name: "SENTINEL", role: "Live Monitoring", icon: "⊲", tier: "primary", description: "Live public-activity tracking." },
  { name: "QUILL", role: "Report Generation", icon: "⟁", tier: "correlation", description: "AI-generated intelligence summaries." },
  { name: "SIGMA", role: "Threat Intelligence", icon: "⊗", tier: "primary", description: "IOC correlation, breach analysis." },
  { name: "EMAIL", role: "Email Intelligence", icon: "✉", tier: "primary", description: "Account existence enumeration." },
  { name: "PHONOS", role: "Phone Intelligence", icon: "☏", tier: "primary", description: "Carrier, line type, region workup." },
] as unknown as readonly AgentCatalogEntry[];

/** The roster used by every view: catalog when available, fallback otherwise. */
export const AGENT_ROSTER: readonly AgentCatalogEntry[] =
  AGENT_CATALOG && AGENT_CATALOG.length > 0 ? AGENT_CATALOG : FALLBACK_AGENTS;

const AGENT_ACCENT: Record<string, string> = {
  APEX: "#00ff88",
  SCOUT: "#00d4ff",
  CRAWLER: "#00d4ff",
  PRISM: "#9966ff",
  IRIS: "#ff66aa",
  ECHO: "#ffb020",
  TERRA: "#44dd88",
  INK: "#aabbff",
  NEXUS: "#ff6644",
  KRONOS: "#ffcc44",
  VAULT: "#88bbff",
  SENTINEL: "#ff4455",
  QUILL: "#ccddff",
  SIGMA: "#ff8833",
  EMAIL: "#00d4ff",
  PHONOS: "#ffcc44",
};

/** Stable accent colour for one agent name. */
export function agentColor(name: string): string {
  return AGENT_ACCENT[name.toUpperCase()] ?? "#5a7a9a";
}

/** Accent colour + label for a terminal agent status. */
export function agentStatusTone(
  status: AgentStatus | "running" | "idle" | null | undefined,
): Tone {
  if (status === "running") return "info";
  if (status === "idle") return "idle";
  return agentTone(status);
}

export interface AgentGridProps {
  /** Agents currently executing (driven by WS `agent` events). */
  activeAgents?: ReadonlySet<string>;
  /** Agents APEX activated for this run. */
  activatedAgents?: ReadonlySet<string>;
  /** Terminal results, used to show DONE/PARTIAL/ERROR. */
  results?: readonly AgentResult[];
  /** Live descriptors from `GET /api/agents`, when loaded. */
  agents?: readonly AgentMeta[];
  onSelect?: (name: string) => void;
  /** Hides the role subtitle in tight sidebars. */
  compact?: boolean;
  className?: string;
  /** Accessible name for the list. */
  label?: string;
}

/**
 * One button per agent, showing running / activated / terminal state.
 *
 * Rendered as a `<ul>` of buttons: clickable when `onSelect` is provided,
 * otherwise as static rows, and never colour-only — each state also carries a
 * text badge.
 */
export function AgentGrid({
  activeAgents = new Set<string>(),
  activatedAgents = new Set<string>(),
  results = [],
  agents = [],
  onSelect,
  compact = false,
  className,
  label = "Intelligence agents",
}: AgentGridProps) {
  const statusByAgent = useMemo(() => {
    const map = new Map<
      string,
      { status: AgentStatus | "running" | "idle"; confidence?: number; latency?: number }
    >();
    for (const result of results) {
      map.set(result.agent.toUpperCase(), {
        status: result.status,
        confidence: result.confidence,
        latency: result.latency_s,
      });
    }
    for (const meta of agents) {
      if (!map.has(meta.name.toUpperCase()) && meta.status) {
        map.set(meta.name.toUpperCase(), { status: meta.status });
      }
    }
    return map;
  }, [results, agents]);

  return (
    <ul
      aria-label={label}
      className={clsx("flex min-w-0 flex-col gap-1", className)}
    >
      {AGENT_ROSTER.map((agent) => {
        const name = agent.name.toUpperCase();
        const isActive = activeAgents.has(name) || activeAgents.has(agent.name);
        const isActivated =
          activatedAgents.has(name) || activatedAgents.has(agent.name);
        const terminal = statusByAgent.get(name);
        const highlight = isActive || isActivated || terminal?.status === "done";
        const color = agentColor(name);

        const state: "RUN" | "DONE" | "PART" | "ERR" | "SKIP" | null = isActive
          ? "RUN"
          : terminal?.status === "done"
            ? "DONE"
            : terminal?.status === "partial"
              ? "PART"
              : terminal?.status === "error"
                ? "ERR"
                : terminal?.status === "skipped"
                  ? "SKIP"
                  : null;

        const row = (
          <>
            <span
              aria-hidden="true"
              className="inline-block h-[5px] w-[5px] shrink-0 rounded-full"
              style={{
                background: highlight ? color : "var(--border-hi)",
                boxShadow: isActive ? `0 0 6px ${color}` : undefined,
              }}
            />
            <span
              aria-hidden="true"
              className="w-5 shrink-0 text-center text-[11px]"
              style={{
                color: highlight ? color : "var(--text-muted)",
                textShadow: highlight ? `0 0 8px ${color}` : undefined,
              }}
            >
              {agent.icon}
            </span>
            <span className="min-w-0 flex-1">
              <span
                className="block truncate font-bold tracking-wide"
                style={{
                  fontSize: 10,
                  letterSpacing: "0.08em",
                  color: highlight ? color : "var(--text)",
                }}
              >
                {agent.name}
              </span>
              {!compact && (
                <span className="mono-label block truncate !text-[8px]">{agent.role}</span>
              )}
            </span>
            {typeof terminal?.latency === "number" && (
              <span className="mono-label shrink-0 !text-[8px] tabular-nums">
                {formatSeconds(terminal.latency)}
              </span>
            )}
            {state && (
              <Badge
                tone={state === "RUN" ? "info" : agentStatusTone(terminal?.status)}
                pulse={state === "RUN"}
                className="shrink-0"
              >
                {state}
              </Badge>
            )}
          </>
        );

        return (
          <li key={name} className="min-w-0">
            {onSelect ? (
              <button
                type="button"
                onClick={() => onSelect(name)}
                title={`${agent.name} — ${agent.role}`}
                className="focus-ring flex w-full min-w-0 items-center gap-2 rounded border px-2 py-1.5 text-left transition-all duration-200"
                style={{
                  background: highlight ? `${color}14` : "transparent",
                  borderColor: highlight ? `${color}44` : "var(--border)",
                }}
              >
                {row}
              </button>
            ) : (
              <div
                className="flex min-w-0 items-center gap-2 rounded border px-2 py-1.5 transition-all duration-200"
                style={{
                  background: highlight ? `${color}14` : "transparent",
                  borderColor: highlight ? `${color}44` : "var(--border)",
                  animation: isActive ? "pulse-green 1.5s ease-in-out infinite" : undefined,
                }}
              >
                {row}
                <StatusDot
                  tone={agentStatusTone(terminal?.status ?? (isActive ? "running" : "idle"))}
                  dotOnly
                  label={`${agent.name} status`}
                  className="sr-only"
                />
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
