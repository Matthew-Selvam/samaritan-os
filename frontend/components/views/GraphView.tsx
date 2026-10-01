"use client";

/**
 * GraphView.tsx — the full-screen entity graph workbench.
 *
 * Wraps `EntityGraph` (which owns the Cytoscape instance) and adds what a view
 * needs around it: data source selection (live run / case / empty), the node
 * inspector, cluster summary, and PNG/JSON export.
 *
 * 2-hop expansion is honest about what it can do: the graph is only as
 * connected as the backend's edges, so when no expansion endpoint exists the
 * button is disabled with an explanation rather than silently doing nothing.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import {
  Badge,
  Button,
  DefRow,
  Drawer,
  EmptyState,
  ErrorState,
  Panel,
  SectionTitle,
  StatTile,
  TextInput,
  useToast,
} from "@/components/ui";
import { EntityBadge } from "@/components/EntityBadge";
import {
  EntityGraph,
  type EntityEdge as GraphEdgeT,
  type EntityNode as GraphNodeT,
  type GraphApi,
  type GraphLayout,
  type NodeSelection,
} from "@/components/EntityGraph";
import { colorFor, labelFor } from "@/lib/entityTypes";
import {
  confidencePct,
  formatRelativeTime,
  truncate,
} from "@/lib/format";
import type { Entity, Graph, Investigation, Signal } from "@/lib/types";
import { downloadBlob, toJson } from "@/lib/hooks";

export interface GraphViewProps {
  /** Entities of the live/selected run. */
  entities: readonly Entity[];
  /** Graph payload when the report carries one (clusters included). */
  graph?: Graph | null;
  signals?: readonly Signal[];
  /** Case entities loaded from CASES → Open in graph. */
  caseEntities?: readonly Entity[] | null;
  onClearCaseEntities?: () => void;
  /** Current run, used for the header. */
  investigation?: Investigation | null;
  /**
   * Entity id to centre on. Set by cross-links from TIMELINE / KNOWLEDGE /
   * CASES; applied once per value, then ignored until it changes.
   */
  focusEntityId?: string | null;
  className?: string;
}

/** Build graph nodes/edges from an entity list (no edges are implied). */
function nodesFromEntities(entities: readonly Entity[]): GraphNodeT[] {
  return entities.map((entity) => ({
    id: entity.id,
    label: entity.label || entity.value || entity.id,
    type: entity.type,
    confidence: entity.confidence,
    attrs: entity.attrs,
    lastSeen: entity.last_seen,
  }));
}

/** Convert a {@link Graph} payload into the component's node/edge shape. */
function nodesFromGraph(graph: Graph): GraphNodeT[] {
  return graph.nodes.map((node) => ({
    id: node.id,
    label: node.label || node.id,
    type: node.type,
    weight: node.weight,
    confidence: node.confidence,
    group: node.group,
  }));
}

function edgesFromGraph(graph: Graph): GraphEdgeT[] {
  return graph.edges.map((edge) => ({
    source: edge.source,
    target: edge.target,
    label: edge.label,
    type: edge.type,
    weight: edge.weight,
    confidence: edge.confidence,
  }));
}

/**
 * Graph workbench.
 *
 * @example
 * <GraphView entities={entities} graph={inv.entities} signals={signals} />
 */
export function GraphView({
  entities,
  graph = null,
  signals = [],
  caseEntities = null,
  onClearCaseEntities,
  investigation = null,
  focusEntityId = null,
  className,
}: GraphViewProps) {
  const toast = useToast();
  const apiRef = useRef<GraphApi | null>(null);
  const [layout, setLayout] = useState<GraphLayout>("cose");
  const [selection, setSelection] = useState<NodeSelection | null>(null);
  const [extraNodes, setExtraNodes] = useState<GraphNodeT[]>([]);
  const [extraEdges, setExtraEdges] = useState<GraphEdgeT[]>([]);

  /* ── Source selection: case override beats run data ── */
  const source: "case" | "run" | "empty" = caseEntities ? "case" : entities.length > 0 || graph ? "run" : "empty";

  const baseNodes = useMemo<GraphNodeT[]>(() => {
    if (source === "case" && caseEntities) return nodesFromEntities(caseEntities);
    if (graph && graph.nodes.length > 0) return nodesFromGraph(graph);
    return nodesFromEntities(entities);
  }, [source, caseEntities, graph, entities]);

  const baseEdges = useMemo<GraphEdgeT[]>(() => {
    if (source === "case") return [];
    if (graph && graph.edges.length > 0) return edgesFromGraph(graph);
    return [];
  }, [source, graph]);

  /* Merged dataset — 2-hop additions live here without touching the source. */
  const allNodes = useMemo(() => {
    const seen = new Set(baseNodes.map((n) => n.id));
    return [...baseNodes, ...extraNodes.filter((n) => !seen.has(n.id))];
  }, [baseNodes, extraNodes]);

  const allEdges = useMemo(() => {
    const seen = new Set(baseEdges.map((e) => `${e.source}→${e.target}`));
    return [...baseEdges, ...extraEdges.filter((e) => !seen.has(`${e.source}→${e.target}`))];
  }, [baseEdges, extraEdges]);

  const clusters = useMemo(() => {
    if (source === "case" || !graph?.clusters) return undefined;
    // Only advertise clusters whose members are actually on the canvas.
    const ids = new Set(allNodes.map((n) => n.id));
    return graph.clusters
      .map((cluster) => ({
        id: cluster.id,
        label: cluster.label,
        members: cluster.members.filter((id) => ids.has(id)),
      }))
      .filter((cluster) => cluster.members.length > 1);
  }, [source, graph, allNodes]);

  const typeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const node of allNodes) {
      const type = (node.type ?? "other") as string;
      counts.set(type, (counts.get(type) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [allNodes]);

  /* ── Expansion: only possible when the payload already carries the hops ── */
  const canExpand = source === "run" && Boolean(graph?.edges?.length);

  const onExpand = useCallback(
    async (nodeId: string): Promise<{ nodes: GraphNodeT[]; edges: GraphEdgeT[] } | null> => {
      if (!graph) return null;
      // Walk the edges locally: the backend exposes no per-node expansion route.
      const firstHop = new Set<string>([nodeId]);
      graph.edges.forEach((edge) => {
        if (edge.source === nodeId) firstHop.add(edge.target);
        if (edge.target === nodeId) firstHop.add(edge.source);
      });
      const secondHop = new Set<string>();
      graph.edges.forEach((edge) => {
        if (firstHop.has(edge.source) && !firstHop.has(edge.target)) secondHop.add(edge.target);
        if (firstHop.has(edge.target) && !firstHop.has(edge.source)) secondHop.add(edge.source);
      });
      const wanted = new Set([...firstHop, ...secondHop]);
      const nodes = graph.nodes
        .filter((node) => wanted.has(node.id))
        .map((node) => ({ id: node.id, label: node.label || node.id, type: node.type, group: node.group }));
      const edges = graph.edges.filter(
        (edge) => firstHop.has(edge.source) || firstHop.has(edge.target),
      );
      toast.push(
        `Expanded ${nodeId} to ${nodes.length} entities within two hops.`,
        "info",
      );
      return { nodes, edges };
    },
    [graph, toast],
  );

  const onMerge = useCallback((added: { nodes: GraphNodeT[]; edges: GraphEdgeT[] }) => {
    setExtraNodes((prev) => [...prev, ...added.nodes]);
    setExtraEdges((prev) => [...prev, ...added.edges]);
  }, []);

  const onReady = useCallback((api: GraphApi) => {
    apiRef.current = api;
  }, []);

  /* ── Cross-link focus: honoured once the node exists on the canvas ── */
  const appliedFocus = useRef<string | null>(null);
  useEffect(() => {
    if (!focusEntityId) {
      appliedFocus.current = null;
      return;
    }
    if (appliedFocus.current === focusEntityId) return;
    const api = apiRef.current;
    if (!api) return;
    // The node can arrive a tick after the view mounts, so retry briefly.
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      if (api.focusNode(focusEntityId)) {
        appliedFocus.current = focusEntityId;
        window.clearInterval(timer);
      } else if (attempts > 12) {
        window.clearInterval(timer);
        toast.error(`Entity ${truncate(focusEntityId, 40)} is not part of the loaded graph.`);
      }
    }, 180);
    return () => window.clearInterval(timer);
  }, [allNodes.length, focusEntityId, toast]);

  const exportPng = useCallback(() => {
    const api = apiRef.current;
    if (!api) return;
    const png = api.toPng({ scale: 2 });
    if (!png) {
      toast.error("The graph canvas could not be rasterised.");
      return;
    }
    const anchor = document.createElement("a");
    anchor.href = png;
    anchor.download = `signal-os-graph-${investigation?.inv_id ?? "export"}.png`;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    toast.success("Graph PNG downloaded.");
  }, [investigation, toast]);

  const exportJson = useCallback(() => {
    const api = apiRef.current;
    const payload = api ? api.toJSON() : { nodes: allNodes, edges: allEdges, clusters };
    downloadBlob(
      toJson(payload),
      `signal-os-graph-${investigation?.inv_id ?? "export"}.json`,
      "application/json",
    );
    toast.success("Graph JSON downloaded.");
  }, [allEdges, allNodes, clusters, investigation, toast]);

  return (
    <div className={clsx("flex min-h-0 flex-1 flex-col", className)}>
      {/* Header strip */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-1 px-3 py-1.5">
        <Badge tone={source === "empty" ? "idle" : "ok"}>
          {source === "case" ? "CASE DATA" : source === "run" ? "RUN DATA" : "NO DATA"}
        </Badge>
        {investigation && (
          <span className="mono-label truncate !text-[9px]">
            {truncate(investigation.input, 48)} · {investigation.inv_id}
          </span>
        )}
        {source === "case" && onClearCaseEntities && (
          <Button size="sm" variant="ghost" onClick={onClearCaseEntities}>
            Back to run data
          </Button>
        )}
        <span className="flex-1" />
        {typeCounts.slice(0, 6).map(([type, count]) => (
          <EntityBadge key={type} type={type} label={`${labelFor(type)} ${count}`} />
        ))}
        <Button size="sm" variant="outline" onClick={exportPng} disabled={allNodes.length === 0}>
          PNG
        </Button>
        <Button size="sm" variant="outline" onClick={exportJson} disabled={allNodes.length === 0}>
          JSON
        </Button>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 p-2 xl:grid-cols-[minmax(0,1fr)_320px]">
        {/* Canvas */}
        <div className="flex min-h-[420px] min-w-0 flex-col rounded-md border border-border-subtle bg-surface-1">
          {source === "empty" ? (
            <div className="flex flex-1 items-center justify-center">
              <EmptyState
                glyph="∞"
                title="NO GRAPH DATA"
                description="Run an investigation, or open a case from the CASES view, to populate the entity graph."
              />
            </div>
          ) : (
            <EntityGraph
              nodes={allNodes}
              edges={allEdges}
              clusters={clusters}
              signals={signals}
              entities={source === "case" ? caseEntities ?? [] : entities}
              layout={layout}
              onLayoutChange={setLayout}
              onSelectNode={setSelection}
              onExpandNeighbourhood={canExpand ? onExpand : undefined}
              onMergeElements={onMerge}
              onReady={onReady}
              hideLegend
              className="min-h-0 flex-1"
              ariaLabel="Entity relationship graph. Use arrow keys to pan, scroll to zoom, and Tab to reach the legend."
              toolbar={
                <>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => apiRef.current?.fit()}
                    title="Fit all nodes"
                  >
                    Fit
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => apiRef.current?.relayout()}
                    title="Re-run layout"
                  >
                    Relayout
                  </Button>
                </>
              }
            />
          )}
        </div>

        {/* Inspector column */}
        <aside className="scroll-thin flex min-h-0 flex-col gap-2 overflow-y-auto">
          <div className="grid grid-cols-2 gap-2">
            <StatTile label="Nodes" value={allNodes.length} tone="info" icon="◉" />
            <StatTile label="Edges" value={allEdges.length} tone="ok" icon="⇄" />
          </div>

          <Panel eyebrow="LEGEND" title="Entity types">
            {typeCounts.length === 0 ? (
              <EmptyState
                compact
                glyph="◌"
                title="NO TYPES"
                description="The legend is built from the entities in the graph. Run an investigation or open a case to populate it."
              />
            ) : (
              <ul className="flex flex-col gap-1">
                {typeCounts.map(([type, count]) => (
                  <li key={type} className="flex items-center justify-between gap-2">
                    <EntityBadge type={type} />
                    <span className="text-[10px] tabular-nums text-ink-muted">{count}</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-[9px] leading-relaxed text-ink-muted">
              Click a type inside the canvas legend to hide or show it. Hover any node for its
              label; click to inspect its edges and evidence.
            </p>
          </Panel>

          {clusters && clusters.length > 0 && (
            <Panel eyebrow="CLUSTERS" title={`${clusters.length} detected`}>
              <ul className="flex flex-col gap-1">
                {clusters.map((cluster) => (
                  <li key={cluster.id} className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate text-[10px] text-ink">{cluster.label}</span>
                    <Badge tone="idle">{cluster.members.length}</Badge>
                  </li>
                ))}
              </ul>
            </Panel>
          )}

          {!canExpand && source === "run" && (
            <Panel eyebrow="NOTE">
              <p className="m-0 text-[10px] leading-relaxed text-ink-muted">
                Two-hop expansion needs edge data. This run reported{" "}
                {allEdges.length === 0 ? "no relationships" : "a partial graph"}, so the expansion
                control stays disabled rather than pretending to fetch more.
              </p>
            </Panel>
          )}
        </aside>
      </div>

      {/* Node inspector */}
      <Drawer
        open={selection !== null}
        onClose={() => setSelection(null)}
        title={selection ? truncate(selection.node.label, 44) : ""}
        width={400}
      >
        {selection && (
          <div className="flex flex-col gap-3 p-3">
            <EntityBadge type={selection.node.type} size="md" />

            <div>
              <DefRow label="Id" mono>
                {selection.node.id}
              </DefRow>
              {selection.node.confidence !== undefined && (
                <DefRow label="Confidence">{confidencePct(selection.node.confidence)}</DefRow>
              )}
              {selection.node.group && <DefRow label="Cluster">{selection.node.group}</DefRow>}
              {selection.node.lastSeen && (
                <DefRow label="Last seen">{formatRelativeTime(selection.node.lastSeen)}</DefRow>
              )}
            </div>

            {selection.node.attrs && Object.keys(selection.node.attrs).length > 0 && (
              <div>
                <SectionTitle>Attributes</SectionTitle>
                <div>
                  {Object.entries(selection.node.attrs)
                    .slice(0, 12)
                    .map(([key, value]) => (
                      <DefRow key={key} label={key} mono>
                        {truncate(String(value), 80)}
                      </DefRow>
                    ))}
                </div>
              </div>
            )}

            <div>
              <SectionTitle>{`Connected entities (${selection.neighbours.length})`}</SectionTitle>
              {selection.neighbours.length === 0 ? (
                <EmptyState compact glyph="◌" title="ISOLATED NODE" description="No edges touch this entity." />
              ) : (
                <ul className="flex list-none flex-col gap-1 p-0">
                  {selection.neighbours.slice(0, 60).map((neighbour) => (
                    <li key={neighbour.id} className="flex items-center gap-2">
                      <EntityBadge type={neighbour.type} showLabel={false} />
                      <span className="min-w-0 flex-1 truncate text-[10px] text-ink">
                        {truncate(neighbour.label, 48)}
                      </span>
                      {neighbour.confidence !== undefined && (
                        <span className="mono-label shrink-0 !text-[8px]">
                          {confidencePct(neighbour.confidence)}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div>
              <SectionTitle>{`Edges (${selection.edges.length})`}</SectionTitle>
              {selection.edges.length === 0 ? (
                <EmptyState
                  compact
                  glyph="◌"
                  title="NO EDGES"
                  description="Nothing correlates this entity to another. Pick a node with edges in the graph to inspect its relationships."
                />
              ) : (
                <ul className="flex list-none flex-col gap-1 p-0">
                  {selection.edges.slice(0, 40).map((edge, index) => (
                    <li key={index} className="flex items-center gap-1.5 text-[10px]">
                      <Badge tone="idle">{edge.label || edge.type || "related"}</Badge>
                      <span className="min-w-0 flex-1 truncate text-ink-muted">
                        {truncate(edge.source, 22)} → {truncate(edge.target, 22)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div>
              <SectionTitle>{`Evidence (${selection.signals.length})`}</SectionTitle>
              {selection.signals.length === 0 ? (
                <EmptyState
                  compact
                  glyph="◌"
                  title="NO LINKED SIGNALS"
                  description="No signal text referenced this entity."
                />
              ) : (
                <ul className="flex list-none flex-col gap-1 p-0">
                  {selection.signals.slice(0, 30).map((signal, index) => (
                    <li key={index} className="flex items-center gap-1.5">
                      <Badge tone="info">{signal.type}</Badge>
                      <span className="min-w-0 flex-1 truncate text-[10px] text-ink-muted">
                        {truncate(JSON.stringify(signal.value ?? ""), 70)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                if (!apiRef.current) return;
                if (apiRef.current.focusNode(selection.node.id)) {
                  toast.push("Centred the canvas on this entity.", "info");
                }
              }}
            >
              Focus on canvas
            </Button>
          </div>
        )}
      </Drawer>
    </div>
  );
}

export { colorFor };
