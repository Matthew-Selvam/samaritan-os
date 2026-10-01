"use client";

/**
 * AgentsView.tsx — the roster, the routing DAG and a standalone agent console.
 *
 * Identity and routing come from `lib/agentCatalog.ts`; live status comes from
 * the pipeline and `GET /api/agents`; the DAG is drawn as plain SVG from
 * `GET /api/agents/registry/graph` (falling back to the catalog's declared
 * dependencies when that route is absent) so it stays crisp at any zoom and is
 * inspectable by screen readers via the adjacent table.
 */

import { useCallback, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import {
  Badge,
  Button,
  DefRow,
  Drawer,
  EmptyState,
  ErrorState,
  Field,
  Meter,
  Panel,
  SectionTitle,
  StatTile,
  Table,
  Tabs,
  TextArea,
  useToast,
  type Column,
} from "@/components/ui";
import { StatusDot } from "@/components/StatusDot";
import { AGENT_ROSTER, agentColor } from "@/components/AgentGrid";
import { apiFetch, listAgents, runAgent } from "@/lib/api";
import {
  confidencePct,
  formatRelativeTime,
  formatSeconds,
  truncate,
  type Tone,
} from "@/lib/format";
import type {
  AgentMeta,
  AgentNode,
  AgentResult,
  Entity,
  Investigation,
  Signal,
} from "@/lib/types";
import { agentsForInputType, isAgentName, type AgentCatalogEntry } from "@/lib/agentCatalog";
import { apiCall, useAsyncResource } from "@/lib/hooks";

type AgentsTab = "roster" | "dag" | "console";

export interface AgentsViewProps {
  investigation: Investigation | null;
  /** Live results from the current run. */
  agentResults?: readonly AgentResult[];
  /** Agents currently mid-run. */
  activeAgents?: ReadonlySet<string>;
  className?: string;
}

/** One laid-out DAG node. */
interface DagNodeLayout {
  agent: string;
  x: number;
  y: number;
  tier: number;
}

/** One DAG edge. */
interface DagEdgeLayout {
  source: string;
  target: string;
}

/** Fallback dependency edges when the registry route is unavailable. */
const FALLBACK_DEPS: Record<string, string[]> = {
  APEX: ["SCOUT", "NEXUS"],
  SCOUT: [],
  CRAWLER: ["SCOUT"],
  PRISM: ["SCOUT", "CRAWLER"],
  IRIS: ["CRAWLER"],
  ECHO: ["CRAWLER"],
  TERRA: ["IRIS", "ECHO"],
  INK: ["CRAWLER"],
  NEXUS: ["SCOUT", "CRAWLER", "PRISM", "SIGMA"],
  KRONOS: ["NEXUS", "SENTINEL"],
  VAULT: ["NEXUS"],
  SENTINEL: ["SCOUT"],
  QUILL: ["KRONOS", "NEXUS", "VAULT"],
  SIGMA: ["SCOUT", "CRAWLER"],
  EMAIL: ["SCOUT"],
  PHONOS: ["SCOUT"],
};

/** Normalise the registry payload into a dependency map. */
function toDeps(nodes: unknown): Map<string, string[]> {
  const deps = new Map<string, string[]>();
  if (!Array.isArray(nodes)) return deps;
  for (const raw of nodes) {
    if (!raw || typeof raw !== "object") continue;
    const node = raw as AgentNode;
    if (typeof node.name !== "string") continue;
    deps.set(
      node.name.toUpperCase(),
      (node.depends_on ?? []).filter((d): d is string => typeof d === "string").map((d) => d.toUpperCase()),
    );
  }
  return deps;
}

/**
 * Agents workbench.
 *
 * @example
 * <AgentsView investigation={inv} agentResults={inv.agents} />
 */
export function AgentsView({
  investigation = null,
  agentResults = [],
  activeAgents = new Set<string>(),
  className,
}: AgentsViewProps) {
  const toast = useToast();
  const [tab, setTab] = useState<AgentsTab>("roster");
  const [detailName, setDetailName] = useState<string | null>(null);
  const [consoleAgent, setConsoleAgent] = useState<string>("SCOUT");
  const [consoleInput, setConsoleInput] = useState("");
  const [consoleContext, setConsoleContext] = useState("");
  const [consoleResult, setConsoleResult] = useState<AgentResult | null>(null);
  const [consoleEntities, setConsoleEntities] = useState<readonly Entity[]>([]);
  const [consoleSignals, setConsoleSignals] = useState<readonly Signal[]>([]);
  const [running, setRunning] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  /* ── Backend descriptors ── */
  const agentsResource = useAsyncResource<AgentMeta[]>(
    (signal) => apiCall(() => listAgents({ signal, timeout_ms: 15_000 }), "/api/agents"),
    { pollMs: 30_000 },
  );

  /* ── Registry DAG ── */
  const graphResource = useAsyncResource<AgentNode[]>(
    async (signal) => {
      const payload = await apiCall<
        AgentNode[] | { nodes?: AgentNode[]; agents?: AgentNode[] }
      >(
        () =>
          apiFetch<AgentNode[] | { nodes?: AgentNode[]; agents?: AgentNode[] }>(
            "/api/agents/registry/graph",
            { signal, timeout_ms: 15_000 },
          ),
        "/api/agents/registry/graph",
      );
      if (Array.isArray(payload)) return payload;
      if (payload && typeof payload === "object") {
        if (Array.isArray(payload.nodes)) return payload.nodes;
        if (Array.isArray(payload.agents)) return payload.agents;
      }
      return [];
    },
    { pollMs: 60_000 },
  );

  const liveByName = useMemo(() => {
    const map = new Map<string, AgentResult>();
    for (const result of agentResults) map.set(result.agent.toUpperCase(), result);
    return map;
  }, [agentResults]);

  const catalogByName = useMemo(() => {
    const map = new Map<string, AgentCatalogEntry>();
    for (const agent of AGENT_ROSTER) map.set(agent.name.toUpperCase(), agent);
    return map;
  }, []);

  /* ── Dependency map: registry wins, catalog/fallback otherwise ── */
  const deps = useMemo(() => {
    const fromRegistry = toDeps(graphResource.data);
    if (fromRegistry.size > 0) return fromRegistry;
    const fromCatalog = new Map<string, string[]>();
    for (const agent of AGENT_ROSTER) {
      const declared = (agent as unknown as { depends_on?: string[] }).depends_on;
      const fallback = FALLBACK_DEPS[agent.name.toUpperCase()];
      const list = (Array.isArray(declared) && declared.length > 0 ? declared : fallback) ?? [];
      fromCatalog.set(agent.name.toUpperCase(), list.map((d) => d.toUpperCase()));
    }
    return fromCatalog;
  }, [graphResource.data]);

  /* ── Layered DAG layout (longest-path depth) ── */
  const layout = useMemo(() => {
    const names = AGENT_ROSTER.map((agent) => agent.name.toUpperCase());
    const present = new Set(names);
    const depth = new Map<string, number>();

    const compute = (name: string, seen: Set<string>): number => {
      if (depth.has(name)) return depth.get(name) as number;
      if (seen.has(name)) return 0; // cycle guard
      seen.add(name);
      const parents = (deps.get(name) ?? []).filter((p) => present.has(p));
      const value = parents.length === 0 ? 0 : Math.max(...parents.map((p) => compute(p, seen))) + 1;
      seen.delete(name);
      depth.set(name, value);
      return value;
    };
    for (const name of names) compute(name, new Set());

    const byTier = new Map<number, string[]>();
    for (const name of names) {
      const tier = depth.get(name) ?? 0;
      byTier.set(tier, [...(byTier.get(tier) ?? []), name]);
    }

    const NODE_W = 128;
    const NODE_H = 40;
    const GAP_X = 44;
    const GAP_Y = 34;
    const nodes: DagNodeLayout[] = [];
    const tiers = [...byTier.keys()].sort((a, b) => a - b);
    let maxRow = 0;
    for (const tier of tiers) {
      const row = (byTier.get(tier) ?? []).sort();
      maxRow = Math.max(maxRow, row.length);
    }
    for (const tier of tiers) {
      const row = (byTier.get(tier) ?? []).sort();
      const totalWidth = row.length * NODE_W + (row.length - 1) * GAP_X;
      row.forEach((name, index) => {
        nodes.push({
          agent: name,
          x: index * (NODE_W + GAP_X),
          y: tier * (NODE_H + GAP_Y),
          tier,
        });
      });
    }

    const positions = new Map(nodes.map((n) => [n.agent, n]));
    const edges: DagEdgeLayout[] = [];
    for (const [name, parents] of deps) {
      for (const parent of parents) {
        if (positions.has(parent) && positions.has(name)) {
          edges.push({ source: parent, target: name });
        }
      }
    }

    const width = Math.max(
      520,
      maxRow * (NODE_W + GAP_X) - GAP_X + 48,
    );
    const height = Math.max(240, (tiers.length || 1) * (NODE_H + GAP_Y) + 24);

    return { nodes, edges, positions, width, height, tiers };
  }, [deps]);

  /* ── Standalone run ── */
  const runStandalone = useCallback(async () => {
    const input = consoleInput.trim();
    if (!input) {
      toast.error("Enter a target for the agent.");
      return;
    }
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setRunning(true);
    setConsoleResult(null);
    setConsoleEntities([]);
    setConsoleSignals([]);
    try {
      let context: Record<string, unknown> | undefined;
      if (consoleContext.trim()) {
        try {
          context = JSON.parse(consoleContext) as Record<string, unknown>;
        } catch {
          toast.error("Context must be valid JSON.");
          return;
        }
      }
      const payload = await runAgent(
        consoleAgent.toLowerCase(),
        { input, ...(context ? { context } : {}) },
        { signal: controller.signal, timeout_ms: 120_000 },
      );
      const result: AgentResult = payload.result ?? {
        agent: payload.agent ?? consoleAgent,
        status: payload.status ?? "done",
        output: payload.output,
      };
      setConsoleResult(result);
      setConsoleEntities(result.entities_found ?? []);
      setConsoleSignals(result.signals ?? []);
      toast.success(`${consoleAgent} finished.`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  }, [consoleAgent, consoleContext, consoleInput, toast]);

  const suggested = useMemo(() => {
    if (isAgentName(consoleInput.trim().toLowerCase())) {
      return agentsForInputType(consoleInput.trim().toLowerCase() as never);
    }
    return agentsForInputType("text" as never);
  }, [consoleInput]);

  const detailEntry = detailName ? catalogByName.get(detailName) : undefined;
  const detailResult = detailName ? liveByName.get(detailName) : undefined;

  const rosterColumns = useMemo<Column<AgentCatalogEntry>[]>(
    () => [
      {
        key: "name",
        header: "Agent",
        width: "132px",
        sortValue: (row) => row.name,
        render: (row) => (
          <span className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="text-[12px]"
              style={{ color: agentColor(row.name) }}
            >
              {row.icon}
            </span>
            <span className="font-bold tracking-wide text-ink">{row.name}</span>
          </span>
        ),
      },
      {
        key: "role",
        header: "Role",
        width: "170px",
        sortValue: (row) => row.role.toLowerCase(),
        render: (row) => (
          <span className="flex flex-col">
            <span className="truncate text-ink">{row.role}</span>
            <span className="mono-label truncate !text-[8px]">{row.tier ?? "primary"}</span>
          </span>
        ),
      },
      {
        key: "description",
        header: "Description",
        hideBelow: "lg",
        render: (row) => (
          <span className="block min-w-0 truncate text-ink-muted" title={row.description}>
            {row.description}
          </span>
        ),
      },
      {
        key: "model",
        header: "Model",
        width: "150px",
        hideBelow: "lg",
        sortValue: (row) => String((row as unknown as { model?: string }).model ?? ""),
        render: (row) => (
          <span className="mono-label !text-[9px]">
            {(row as unknown as { model?: string }).model ?? "—"}
          </span>
        ),
      },
      {
        key: "status",
        header: "Status",
        width: "104px",
        sortValue: (row) => {
          const result = liveByName.get(row.name.toUpperCase());
          return result?.status ?? (activeAgents.has(row.name.toUpperCase()) ? "running" : "idle");
        },
        render: (row) => {
          const name = row.name.toUpperCase();
          const result = liveByName.get(name);
          const runningNow = activeAgents.has(name);
          if (result) {
            return (
              <Badge
                tone={result.status === "done" ? "ok" : result.status === "partial" ? "warn" : result.status === "error" ? "err" : "idle"}
                pulse={result.status === "partial"}
              >
                {result.status.toUpperCase()}
              </Badge>
            );
          }
          if (runningNow) {
            return (
              <Badge tone="info" pulse>
                RUNNING
              </Badge>
            );
          }
          return <Badge tone="idle">IDLE</Badge>;
        },
      },
      {
        key: "actions",
        header: "",
        width: "132px",
        align: "right",
        render: (row) => (
          <span className="flex justify-end gap-1">
            <Button size="sm" variant="ghost" onClick={() => setDetailName(row.name.toUpperCase())}>
              Detail
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setConsoleAgent(row.name);
                setTab("console");
              }}
            >
              Run
            </Button>
          </span>
        ),
      },
    ],
    [activeAgents, liveByName],
  );

  return (
    <div className={clsx("flex min-h-0 flex-1 flex-col", className)}>
      <Panel bodyClassName="!p-0">
        <Tabs
          label="Agent views"
          active={tab}
          onChange={setTab}
          items={[
            { id: "roster", label: "Roster", count: AGENT_ROSTER.length },
            { id: "dag", label: "Routing DAG", count: layout.edges.length },
            { id: "console", label: "Console" },
          ]}
          actions={
            agentsResource.refreshing ? (
              <Badge tone="info" pulse>
                LIVE
              </Badge>
            ) : (
              <Badge tone="idle">{agentsResource.data?.length ?? AGENT_ROSTER.length} registered</Badge>
            )
          }
        />

        {/* ── Roster ── */}
        {tab === "roster" && (
          <div className="p-2">
            {agentsResource.state === "error" && (
              <p className="m-0 mb-2 text-[10px] text-signal-warn">
                Live agent status unavailable ({truncate(agentsResource.error ?? "", 90)}); showing the
                catalog roster.
              </p>
            )}
            <Table
              columns={rosterColumns}
              rows={AGENT_ROSTER as readonly AgentCatalogEntry[]}
              rowKey={(row) => row.name}
              onRowClick={(row) => setDetailName(row.name.toUpperCase())}
              caption="Agent roster"
              defaultSort={{ key: "name", direction: "asc" }}
            />
          </div>
        )}

        {/* ── DAG ── */}
        {tab === "dag" && (
          <div className="grid min-h-0 grid-cols-1 gap-2 p-2 xl:grid-cols-[minmax(0,1fr)_340px]">
            <div className="flex min-h-[420px] flex-col gap-2">
              <Panel
                eyebrow="ROUTING"
                title="Dependency graph"
                actions={
                  <>
                    <Badge tone="idle">{layout.nodes.length} NODES</Badge>
                    <Badge tone="idle">{layout.edges.length} EDGES</Badge>
                    {graphResource.state === "error" && (
                      <Badge tone="warn" title="Registry route unavailable — using the local dependency map">
                        FALLBACK MAP
                      </Badge>
                    )}
                  </>
                }
                bodyClassName="!p-0"
              >
                <div className="scroll-thin hairline-grid-dense overflow-auto p-2">
                  <svg
                    width={layout.width}
                    height={layout.height}
                    viewBox={`0 0 ${layout.width} ${layout.height}`}
                    role="img"
                    aria-label={`Agent routing DAG with ${layout.nodes.length} agents and ${layout.edges.length} dependencies`}
                  >
                    <defs>
                      <marker
                        id="dag-arrow"
                        viewBox="0 0 10 10"
                        refX="9"
                        refY="5"
                        markerWidth="6"
                        markerHeight="6"
                        orient="auto-start-reverse"
                      >
                        <path d="M 0 0 L 10 5 L 0 10 z" fill="#2a4060" />
                      </marker>
                    </defs>

                    {/* Edges first so nodes paint on top */}
                    {layout.edges.map((edge, index) => {
                      const from = layout.positions.get(edge.source);
                      const to = layout.positions.get(edge.target);
                      if (!from || !to) return null;
                      const x1 = from.x + 64;
                      const y1 = from.y + 40;
                      const x2 = to.x + 64;
                      const y2 = to.y;
                      const midY = (y1 + y2) / 2;
                      const active = activeAgents.has(edge.source) || activeAgents.has(edge.target);
                      return (
                        <path
                          key={`${edge.source}-${edge.target}-${index}`}
                          d={`M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}`}
                          fill="none"
                          stroke={active ? agentColor(edge.target) : "#2a4060"}
                          strokeWidth={active ? 1.6 : 1}
                          markerEnd="url(#dag-arrow)"
                          opacity={active ? 0.95 : 0.7}
                        />
                      );
                    })}

                    {/* Nodes */}
                    {layout.nodes.map((node) => {
                      const color = agentColor(node.agent);
                      const result = liveByName.get(node.agent);
                      const running = activeAgents.has(node.agent);
                      const tone: Tone = result
                        ? result.status === "done"
                          ? "ok"
                          : result.status === "partial"
                            ? "warn"
                            : result.status === "error"
                              ? "err"
                              : "idle"
                        : running
                          ? "info"
                          : "idle";
                      return (
                        <g
                          key={node.agent}
                          transform={`translate(${node.x}, ${node.y})`}
                          className="cursor-pointer"
                          onClick={() => setDetailName(node.agent)}
                          role="button"
                          tabIndex={0}
                          onKeyDown={(event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              setDetailName(node.agent);
                            }
                          }}
                        >
                          <rect
                            width={128}
                            height={40}
                            rx={4}
                            fill={running ? `${color}1f` : "#111820"}
                            stroke={result || running ? color : "#1e2d3d"}
                            strokeWidth={running ? 1.8 : 1}
                          />
                          <text
                            x={64}
                            y={17}
                            textAnchor="middle"
                            fill={color}
                            fontSize={10}
                            fontWeight={700}
                            fontFamily="var(--font-mono)"
                            letterSpacing="0.08em"
                          >
                            {node.agent}
                          </text>
                          <text
                            x={64}
                            y={30}
                            textAnchor="middle"
                            fill="#5a7a9a"
                            fontSize={7.5}
                            fontFamily="var(--font-mono)"
                          >
                            {truncate(catalogByName.get(node.agent)?.role ?? "", 20)}
                          </text>
                          <circle cx={120} cy={9} r={3.5} fill={TONE_HEX[tone]} />
                        </g>
                      );
                    })}
                  </svg>
                </div>
              </Panel>
            </div>

            <aside className="flex min-h-0 flex-col gap-2">
              <Panel eyebrow="DEPENDENCIES" title="Declared edges" bodyClassName="!p-0">
                {layout.edges.length === 0 ? (
                  <EmptyState
                    compact
                    glyph="◌"
                    title="NO EDGES REPORTED"
                    description="Agent-to-agent dependencies appear once the backend reports its orchestration graph for this run."
                  />
                ) : (
                  <div className="scroll-thin max-h-[420px] overflow-y-auto">
                    <ul className="flex list-none flex-col">
                      {layout.edges.map((edge, index) => (
                        <li
                          key={`${edge.source}-${edge.target}-${index}`}
                          className="flex items-center gap-1.5 border-b border-border-subtle/60 px-2 py-1 text-[10px]"
                        >
                          <span style={{ color: agentColor(edge.source) }}>{edge.source}</span>
                          <span aria-hidden="true" className="text-ink-muted">
                            →
                          </span>
                          <span style={{ color: agentColor(edge.target) }}>{edge.target}</span>
                          <span className="mono-label ml-auto !text-[8px]">
                            {truncate(
                              catalogByName.get(edge.source)?.role ?? "",
                              18,
                            )}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </Panel>

              <Panel eyebrow="ROUTING HINTS" title="By input type">
                <ul className="m-0 flex list-none flex-col gap-1 p-0">
                  {(["email", "domain", "ip", "username", "wallet", "image", "text"] as const).map(
                    (inputType) => {
                      const list = agentsForInputType(inputType as never);
                      return (
                        <li key={inputType}>
                          <DefRow label={inputType} mono>
                            {list.length > 0 ? list.join(" · ") : "—"}
                          </DefRow>
                        </li>
                      );
                    },
                  )}
                </ul>
              </Panel>
            </aside>
          </div>
        )}

        {/* ── Console ── */}
        {tab === "console" && (
          <div className="grid grid-cols-1 gap-2 p-2 xl:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
            <Panel eyebrow="CONSOLE" title="Run a single agent">
              <div className="flex flex-col gap-2">
                <Field label="Agent" htmlFor="console-agent">
                  <select
                    id="console-agent"
                    value={consoleAgent}
                    onChange={(event) => setConsoleAgent(event.target.value)}
                    className="focus-ring rounded-md border border-border-strong bg-surface-0 px-2 py-1.5 text-[12px] text-ink"
                  >
                    {AGENT_ROSTER.map((agent) => (
                      <option key={agent.name} value={agent.name}>
                        {agent.icon} {agent.name} — {agent.role}
                      </option>
                    ))}
                  </select>
                </Field>

                <Field label="Input" htmlFor="console-input">
                  <TextArea
                    id="console-input"
                    rows={3}
                    value={consoleInput}
                    onChange={(event) => setConsoleInput(event.target.value)}
                    placeholder="target@example.com"
                  />
                </Field>

                <Field
                  label="Context (JSON)"
                  htmlFor="console-context"
                  hint={"Optional agent overrides, e.g. " + JSON.stringify({ deep: true }) + "."}
                >
                  <TextArea
                    id="console-context"
                    rows={3}
                    value={consoleContext}
                    onChange={(event) => setConsoleContext(event.target.value)}
                    placeholder="{}"
                    spellCheck={false}
                  />
                </Field>

                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    variant="solid"
                    onClick={() => void runStandalone()}
                    loading={running}
                    disabled={!consoleInput.trim()}
                  >
                    Run {consoleAgent}
                  </Button>
                  {running && (
                    <Button
                      variant="danger"
                      onClick={() => {
                        abortRef.current?.abort();
                        toast.push("Run cancelled.", "warn");
                      }}
                    >
                      Cancel
                    </Button>
                  )}
                </div>

                {suggested.length > 0 && (
                  <p className="m-0 text-[9px] text-ink-muted">
                    APEX would route similar input to:{" "}
                    {suggested.map((agent) => agent.name).join(" · ")}
                  </p>
                )}
              </div>
            </Panel>

            <Panel
              eyebrow="OUTPUT"
              title={consoleResult ? `${consoleResult.agent} result` : "Result"}
              actions={
                consoleResult ? (
                  <>
                    <Badge
                      tone={
                        consoleResult.status === "done"
                          ? "ok"
                          : consoleResult.status === "partial"
                            ? "warn"
                            : consoleResult.status === "error"
                              ? "err"
                              : "idle"
                      }
                    >
                      {consoleResult.status.toUpperCase()}
                    </Badge>
                    {consoleResult.confidence !== undefined && (
                      <Badge tone="ok">{confidencePct(consoleResult.confidence)}</Badge>
                    )}
                    <Badge tone="idle">{formatSeconds(consoleResult.latency_s)}</Badge>
                  </>
                ) : undefined
              }
            >
              {running ? (
                <EmptyState
                  compact
                  glyph="◷"
                  title={`${consoleAgent} RUNNING…`}
                  description="The standalone console bypasses APEX, so this result arrives on its own rather than as part of a fused investigation."
                />
              ) : consoleResult === null ? (
                <EmptyState
                  glyph="⌨"
                  title="NO RUN YET"
                  description="Pick an agent, give it a target and press Run. Standalone runs bypass APEX, so they are the fastest way to test one capability."
                />
              ) : consoleResult.error ? (
                <ErrorState
                  compact
                  title={`${consoleResult.agent} failed`}
                  message={consoleResult.error}
                  onRetry={() => void runStandalone()}
                />
              ) : (
                <div className="flex flex-col gap-3">
                  {consoleResult.reasoning && (
                    <div>
                      <SectionTitle>Reasoning</SectionTitle>
                      <p className="m-0 whitespace-pre-wrap text-[11px] leading-relaxed text-ink-muted">
                        {consoleResult.reasoning}
                      </p>
                    </div>
                  )}

                  <div className="grid gap-2 sm:grid-cols-3">
                    <StatTile
                      label="Entities"
                      value={consoleEntities.length}
                      tone="info"
                      icon="◉"
                    />
                    <StatTile label="Signals" value={consoleSignals.length} tone="warn" icon="≋" />
                    <StatTile
                      label="Confidence"
                      value={confidencePct(consoleResult.confidence)}
                      tone={consoleResult.confidence === undefined ? "idle" : "ok"}
                      progress={consoleResult.confidence}
                    />
                  </div>

                  {consoleResult.steps && consoleResult.steps.length > 0 && (
                    <div>
                      <SectionTitle>{`Steps (${consoleResult.steps.length})`}</SectionTitle>
                      <ol className="m-0 flex list-none flex-col gap-1 p-0">
                        {consoleResult.steps.map((step, index) => (
                          <li key={index} className="flex gap-2 text-[10px]">
                            <span className="w-5 shrink-0 text-ink-muted tabular-nums">
                              {String(index + 1).padStart(2, "0")}
                            </span>
                            <span className="min-w-0 break-words text-ink-muted">{step}</span>
                          </li>
                        ))}
                      </ol>
                    </div>
                  )}

                  {consoleEntities.length > 0 && (
                    <div>
                      <SectionTitle>{`Entities (${consoleEntities.length})`}</SectionTitle>
                      <ul className="m-0 flex list-none flex-col gap-1 p-0">
                        {consoleEntities.slice(0, 60).map((entity) => (
                          <li key={entity.id} className="flex items-center gap-2 text-[10px]">
                            <span className="min-w-0 flex-1 truncate text-ink">
                              {truncate(entity.label || entity.value || entity.id, 60)}
                            </span>
                            <span className="mono-label shrink-0 !text-[8px]">{entity.type}</span>
                            <span className="mono-label shrink-0 !text-[8px]">
                              {confidencePct(entity.confidence)}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {consoleSignals.length > 0 && (
                    <div>
                      <SectionTitle>{`Signals (${consoleSignals.length})`}</SectionTitle>
                      <ul className="m-0 flex list-none flex-col gap-1 p-0">
                        {consoleSignals.slice(0, 40).map((signal, index) => (
                          <li key={index} className="flex items-center gap-2 text-[10px]">
                            <Badge tone="info">{signal.type}</Badge>
                            <span className="min-w-0 flex-1 truncate text-ink-muted">
                              {truncate(JSON.stringify(signal.value ?? ""), 60)}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  <div>
                    <SectionTitle>Raw output</SectionTitle>
                    <pre className="scroll-thin m-0 max-h-[300px] overflow-auto rounded border border-border-subtle bg-surface-0 p-2 text-[9px] leading-relaxed text-ink-muted">
                      {JSON.stringify(consoleResult.output ?? null, null, 2)}
                    </pre>
                  </div>
                </div>
              )}
            </Panel>
          </div>
        )}
      </Panel>

      {/* Agent detail drawer */}
      <Drawer
        open={detailName !== null}
        onClose={() => setDetailName(null)}
        title={detailEntry ? `${detailEntry.icon} ${detailEntry.name}` : ""}
        width={400}
      >
        {detailEntry && (
          <div className="flex flex-col gap-3 p-3">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone="idle">{detailEntry.role}</Badge>
              <Badge tone="info">{detailEntry.tier ?? "primary"}</Badge>
              {(detailEntry as unknown as { model?: string }).model && (
                <Badge tone="idle">
                  {(detailEntry as unknown as { model?: string }).model}
                </Badge>
              )}
              {detailResult && (
                <Badge
                  tone={
                    detailResult.status === "done"
                      ? "ok"
                      : detailResult.status === "partial"
                        ? "warn"
                        : detailResult.status === "error"
                          ? "err"
                          : "idle"
                  }
                >
                  {detailResult.status.toUpperCase()}
                </Badge>
              )}
            </div>

            <p className="m-0 text-[11px] leading-relaxed text-ink-muted">{detailEntry.description}</p>

            <div>
              <SectionTitle>Routing</SectionTitle>
              <DefRow label="Depends on">
                {(deps.get(detailEntry.name.toUpperCase()) ?? []).join(" · ") || "—"}
              </DefRow>
              <DefRow label="Dependents">
                {[...deps.entries()]
                  .filter(([, parents]) => parents.includes(detailEntry.name.toUpperCase()))
                  .map(([name]) => name)
                  .join(" · ") || "—"}
              </DefRow>
              <DefRow label="Handles input">
                {(detailEntry as unknown as { inputTypes?: string[] }).inputTypes?.join(", ") ?? "—"}
              </DefRow>
            </div>

            {detailResult && (
              <div>
                <SectionTitle>Last result</SectionTitle>
                {detailResult.error ? (
                  <p className="m-0 break-words text-[10px] text-signal-err">{detailResult.error}</p>
                ) : (
                  <>
                    <DefRow label="Confidence">{confidencePct(detailResult.confidence)}</DefRow>
                    <DefRow label="Latency">{formatSeconds(detailResult.latency_s)}</DefRow>
                    <DefRow label="Entities">{detailResult.entities_found?.length ?? 0}</DefRow>
                    <DefRow label="Signals">{detailResult.signals?.length ?? 0}</DefRow>
                    {detailResult.tokens_used !== undefined && (
                      <DefRow label="Tokens">{detailResult.tokens_used}</DefRow>
                    )}
                  </>
                )}
                {detailResult.confidence !== undefined && (
                  <Meter
                    value={detailResult.confidence}
                    tone={detailResult.confidence >= 0.7 ? "ok" : "warn"}
                    className="mt-1"
                    label={`${detailEntry.name} confidence`}
                  />
                )}
              </div>
            )}

            {!detailResult && investigation?.agents_activated?.includes(detailEntry.name) && (
              <StatusDot
                tone={activeAgents.has(detailEntry.name.toUpperCase()) ? "info" : "idle"}
                label={
                  activeAgents.has(detailEntry.name.toUpperCase())
                    ? "Running now"
                    : "Activated for this run"
                }
              />
            )}

            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setConsoleAgent(detailEntry.name);
                  setTab("console");
                  setDetailName(null);
                }}
              >
                Open in console
              </Button>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}

/** Tone → hex, mirroring `lib/format.toneColor` for inline SVG. */
const TONE_HEX: Record<Tone, string> = {
  ok: "#00ff88",
  warn: "#ffb020",
  err: "#ff4455",
  info: "#00d4ff",
  idle: "#5a7a9a",
};

export { formatRelativeTime };
