"use client";

/**
 * EntityGraph.tsx — the force-directed knowledge graph.
 *
 * Cytoscape is instantiated once per dataset and then mutated in place:
 * layout runs at most once, type visibility / selection / search are class
 * toggles, and 2-hop expansion adds elements incrementally. That is what keeps
 * it smooth past 1000 nodes — rebuilding the graph on every filter change was
 * the previous implementation's bottleneck.
 *
 * Everything the shell needs (search hit list, PNG/JSON export, per-node
 * detail) is exposed through callbacks rather than internal state so the view
 * layer can compose it with the rest of the app.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import cytoscapeCtor, {
  type Core,
  type ElementDefinition,
  type EventObjectNode,
  type EventObjectEdge,
  type NodeSingular,
} from "cytoscape";
import {
  ENTITY_TYPES,
  ENTITY_GROUP_ORDER,
  colorFor,
  labelFor,
  normalizeEntityType,
  typesInGroup,
  type EntityGroup,
} from "@/lib/entityTypes";
import type { EntityType, Graph, GraphEdge, GraphNode, Signal, Entity } from "@/lib/types";
import { confidencePct, truncate, truncateMiddle } from "@/lib/format";
import { Badge, Button, EmptyState, Panel, Segmented, TextInput, Tooltip } from "./ui";

/* ── Public types ───────────────────────────────────────────────────────────── */

/** Node shape accepted by the graph (a superset of {@link GraphNode}). */
export interface EntityNode {
  id: string;
  label: string;
  type: string;
  weight?: number;
  confidence?: number;
  group?: string;
  /** Extra detail surfaced in the inspector. */
  attrs?: Record<string, unknown>;
  lastSeen?: string;
}

/** Edge shape accepted by the graph (a superset of {@link GraphEdge}). */
export interface EntityEdge {
  source: string;
  target: string;
  label?: string;
  type?: string;
  weight?: number;
  confidence?: number;
}

/** Layout engines offered by the layout switcher. */
export type GraphLayout = "cose" | "cose-bilkent" | "circle" | "concentric" | "grid" | "breadthfirst";

/** Callback payload for a node click. */
export interface NodeSelection {
  node: EntityNode;
  /** Edges touching the node (both directions). */
  edges: EntityEdge[];
  /** Immediate and 2-hop neighbours. */
  neighbours: EntityNode[];
  /** Signals attributed to this entity, when the caller can supply them. */
  signals: Signal[];
}

/** CoSE-Bilkent is loaded lazily; it is only needed when selected. */
interface CoseBilkentLayout {
  name: string;
  animate?: boolean;
  animationDuration?: number;
  padding?: number;
  nodeRepulsion?: () => number;
  idealEdgeLength?: () => number;
  edgeElasticity?: () => number;
  nestingFactor?: () => number;
  gravity?: number;
  numIter?: number;
  tile?: boolean;
}

/** True once the extension has registered itself on the Cytoscape prototype. */
let coseBilkentReady = false;
let coseBilkentLoader: Promise<unknown> | null = null;

/**
 * Dynamically import `cytoscape-cose-bilkent` and register it exactly once.
 *
 * The package ships no type declarations, hence the `unknown` cast — the only
 * `any`-free way to consume it. Failure is non-fatal: the caller falls back to
 * the built-in `cose` layout.
 */
async function ensureCoseBilkent(): Promise<boolean> {
  if (coseBilkentReady) return true;
  coseBilkentLoader ??= import("cytoscape-cose-bilkent")
    .then((mod) => {
      const register: unknown = (mod as { default?: unknown }).default ?? mod;
      if (typeof register === "function") {
        (register as (cy: Core) => void)(cytoscapeStatic());
      }
      coseBilkentReady = true;
      return true;
    })
    .catch((err: unknown) => {
      console.warn("[signal-os] cose-bilkent layout unavailable:", err);
      return false;
    });
  return (await coseBilkentLoader) === true;
}

/**
 * The live Cytoscape constructor. Registration mutates its prototype, so the
 * same instance the app already uses is the one handed to the extension.
 */
function cytoscapeStatic(): Core {
  return cytoscapeCtor({ headless: true, elements: [], styleEnabled: false });
}

/** Node/edge count above which labels stop being drawn (readability + speed). */
const LABEL_BUDGET = 120;

/** Render options. */
export interface EntityGraphProps {
  nodes: readonly EntityNode[];
  edges: readonly EntityEdge[];
  /** Clusters drawn as compound parents when supplied. */
  clusters?: readonly { id: string; label: string; members: readonly string[] }[];
  /** Signals used to populate the inspector's evidence list. */
  signals?: readonly Signal[];
  /** Entity records keyed by `id`, used to enrich the inspector. */
  entities?: readonly Entity[];
  /** Layout engine; changes re-run the layout only, never the graph. */
  layout?: GraphLayout;
  onLayoutChange?: (layout: GraphLayout) => void;
  /** Fired on node click / keyboard activation. */
  onSelectNode?: (selection: NodeSelection | null) => void;
  /** Extra element pairs to add to the canvas (2-hop expansion). */
  onExpandNeighbourhood?: (
    nodeId: string,
  ) => Promise<{ nodes: EntityNode[]; edges: EntityEdge[] } | null>;
  /** Merge newly fetched elements into the controlled dataset. */
  onMergeElements?: (added: { nodes: EntityNode[]; edges: EntityEdge[] }) => void;
  /** Rendered above the canvas (search bar, filters, actions). */
  toolbar?: React.ReactNode;
  /** Hides the built-in legend — the GRAPH view supplies its own panel. */
  hideLegend?: boolean;
  className?: string;
  /** Accessible description of the canvas. */
  ariaLabel?: string;
  /** Called once the canvas is mounted, so the shell can trigger exports. */
  onReady?: (api: GraphApi) => void;
}

/** Imperative handle the view can hold for export / programmatic control. */
export interface GraphApi {
  /** PNG data URL of the current viewport. */
  toPng: (opts?: { scale?: number }) => string | null;
  /** Serializable snapshot of what is on the canvas. */
  toJSON: () => Graph;
  /** Fit the viewport to the visible nodes. */
  fit: () => void;
  /** Focus a node by id (select + centre + flash). */
  focusNode: (id: string) => boolean;
  /** Re-run the current layout. */
  relayout: () => void;
  /** Visible / hidden node counts. */
  stats: () => { nodes: number; edges: number; hidden: number };
}

/* ── Element construction ───────────────────────────────────────────────────── */

/** Build Cytoscape node elements with the shared entity palette. */
function toNodeElements(nodes: readonly EntityNode[]): ElementDefinition[] {
  return nodes.map((node) => {
    const type = normalizeEntityType(node.type);
    return {
      group: "nodes" as const,
      data: {
        id: node.id,
        label: node.label,
        entityType: type,
        color: colorFor(type),
        weight: typeof node.weight === "number" ? node.weight : 1,
        confidence: typeof node.confidence === "number" ? node.confidence : null,
        cluster: node.group ?? null,
      },
    };
  });
}

/** Build Cytoscape edge elements, skipping dangling endpoints. */
function toEdgeElements(
  edges: readonly EntityEdge[],
  known: ReadonlySet<string>,
): ElementDefinition[] {
  const out: ElementDefinition[] = [];
  const seen = new Set<string>();
  edges.forEach((edge, index) => {
    if (!known.has(edge.source) || !known.has(edge.target)) return;
    // Parallel edges are common in a correlation graph; keep the first only.
    const key = `${edge.source}→${edge.target}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({
      group: "edges" as const,
      data: {
        id: `e${index}:${edge.source}→${edge.target}`,
        source: edge.source,
        target: edge.target,
        label: edge.label ?? edge.type ?? "",
        edgeType: edge.type ?? "related",
        confidence: typeof edge.confidence === "number" ? edge.confidence : null,
      },
    });
  });
  return out;
}

/** Node size from weight, clamped so 1000-node views stay legible. */
function sizeStyle(cy: Core): string {
  return cy
    .nodes(":visible")
    .map((node: NodeSingular) => {
      const weight = Number(node.data("weight") ?? 1);
      const size = 14 + Math.min(30, Math.sqrt(Math.max(0, weight)) * 9);
      return `${node.id()}: ${size.toFixed(1)}`;
    })
    .join("; ");
}

/* ── Stylesheet ─────────────────────────────────────────────────────────────── */

function buildStylesheet(labelBudget: number): cytoscape.StylesheetStyle[] {
  return [
    {
      selector: "node",
      style: {
        "background-color": "data(color)",
        "background-opacity": 0.22,
        "border-width": 1.5,
        "border-color": "data(color)",
        label: "data(label)",
        color: "#c8d8e8",
        "font-size": 9,
        "font-family": "JetBrains Mono, ui-monospace, monospace",
        "text-valign": "bottom",
        "text-halign": "center",
        "text-margin-y": 4,
        width: 24,
        height: 24,
        "text-max-width": "80px",
        "text-wrap": "ellipsis",
        "min-zoomed-font-size": 7,
      },
    },
    {
      selector: "edge",
      style: {
        width: 1,
        "line-color": "#2a4060",
        "target-arrow-color": "#2a4060",
        "target-arrow-shape": "triangle",
        "curve-style": "bezier",
        label: "data(label)",
        "font-size": 8,
        "font-family": "JetBrains Mono, ui-monospace, monospace",
        color: "#5a7a9a",
        "text-rotation": "autorotate",
        "text-background-color": "#0d1117",
        "text-background-opacity": 0.9,
        "text-background-padding": "2px",
        "min-zoomed-font-size": 6,
      },
    },
    { selector: "node:selected", style: { "background-opacity": 0.45, "border-width": 3 } },
    {
      selector: "edge:selected",
      style: { "line-color": "#00d4ff", "target-arrow-color": "#00d4ff" },
    },
    // Search matches stay bright; everything else dims.
    {
      selector: "node.dimmed",
      style: { opacity: 0.12, "text-opacity": 0 },
    },
    {
      selector: "edge.dimmed",
      style: { opacity: 0.08, "text-opacity": 0 },
    },
    {
      selector: "node.match",
      style: {
        "border-width": 3,
        "background-opacity": 0.5,
        "border-color": "#ffffff",
        "z-index": 10,
      },
    },
    {
      selector: "node.neighbour",
      style: { "background-opacity": 0.35, "border-width": 2 },
    },
    { selector: "node.hidden-by-filter", style: { display: "none" } },
    { selector: "edge.hidden-by-filter", style: { display: "none" } },
    // Clusters are compound parents.
    {
      selector: "node.cluster",
      style: {
        "background-opacity": 0.05,
        "border-width": 1,
        "border-style": "dashed",
        "border-color": "#2a4060",
        label: "data(label)",
        color: "#5a7a9a",
        "font-size": 10,
        "text-valign": "top",
        "text-halign": "center",
        shape: "round-rectangle",
      },
    },
    { selector: "node:parent", style: { opacity: 0.9 } },
  ];
}

/* ── Component ──────────────────────────────────────────────────────────────── */

/**
 * Interactive entity graph.
 *
 * @example
 * <EntityGraph nodes={nodes} edges={edges} onSelectNode={setSelection} />
 */
export function EntityGraph({
  nodes,
  edges,
  clusters,
  signals = [],
  entities = [],
  layout = "cose",
  onLayoutChange,
  onSelectNode,
  onExpandNeighbourhood,
  onMergeElements,
  toolbar,
  hideLegend = false,
  className,
  ariaLabel = "Entity relationship graph",
  onReady,
}: EntityGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const [ready, setReady] = useState(false);
  const [visibleTypes, setVisibleTypes] = useState<ReadonlySet<EntityType>>(
    () => new Set(ENTITY_TYPES),
  );
  const [query, setQuery] = useState("");
  const [matchCount, setMatchCount] = useState(0);
  const [expanding, setExpanding] = useState(false);

  // Latest props in refs so the Cytoscape effect never re-runs for them.
  const onSelectRef = useRef(onSelectNode);
  onSelectRef.current = onSelectNode;
  const onExpandRef = useRef(onExpandNeighbourhood);
  onExpandRef.current = onExpandNeighbourhood;
  const onMergeRef = useRef(onMergeElements);
  onMergeRef.current = onMergeElements;
  const signalsRef = useRef(signals);
  signalsRef.current = signals;
  const entitiesRef = useRef(entities);
  entitiesRef.current = entities;
  const layoutRef = useRef(layout);
  layoutRef.current = layout;

  /** Entity metadata for the inspector, keyed by node id. */
  const entityById = useMemo(() => {
    const map = new Map<string, Entity>();
    for (const entity of entities) map.set(entity.id, entity);
    return map;
  }, [entities]);

  /* ── Graph construction (data identity changes only) ── */
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    cyRef.current?.destroy();
    cyRef.current = null;
    setReady(false);

    if (nodes.length === 0) return;

    const known = new Set(nodes.map((n) => n.id));
    const nodeElements: ElementDefinition[] = toNodeElements(nodes);
    const edgeElements = toEdgeElements(edges, known);

    // Compound parents for clusters, when provided.
    if (clusters && clusters.length > 0) {
      const members = new Set<string>();
      for (const cluster of clusters) {
        for (const id of cluster.members) members.add(id);
      }
      for (const cluster of clusters) {
        nodeElements.push({
          group: "nodes",
          data: { id: `cluster:${cluster.id}`, label: cluster.label, isCluster: true, cluster: null },
          classes: "cluster",
        });
      }
      for (const node of nodeElements) {
        const data = node.data as Record<string, unknown> | undefined;
        if (!data || typeof data !== "object" || !("id" in data)) continue;
        const id = String(data.id);
        if (id.startsWith("cluster:")) continue;
        const owner = clusters.find((c) => c.members.includes(id));
        if (owner) {
          // `parent` is only present on NodeDefinition; cast through the
          // writable shape rather than widening ElementDefinition.
          (node as { parent?: string }).parent = `cluster:${owner.id}`;
          data.cluster = owner.label;
        }
      }
      void members;
    }

    const cy: Core = cytoscapeCtor({
      container,
      elements: [...nodeElements, ...edgeElements],
      style: buildStylesheet(nodes.length > LABEL_BUDGET ? 0 : 1),
      wheelSensitivity: 0.2,
      pixelRatio: "auto",
      boxSelectionEnabled: false,
      autoungrabify: false,
    });

    cyRef.current = cy;
    cy.one("ready", () => setReady(true));

    /* ── Node selection → inspector ── */
    const describe = (node: NodeSingular): NodeSelection | null => {
      const data = node.data() as unknown as Record<string, unknown>;
      const meta = entityById.get(node.id());
      const typed: EntityNode = {
        id: node.id(),
        label: String(data.label ?? node.id()),
        type: normalizeEntityType(String(data.entityType ?? "other")),
        weight: typeof data.weight === "number" ? data.weight : undefined,
        confidence: typeof data.confidence === "number" ? data.confidence : undefined,
        group: typeof data.cluster === "string" ? data.cluster : undefined,
        attrs: meta?.attrs,
        lastSeen: meta?.last_seen,
      };
      const incident = cy.edges().filter((e) => e.source().id() === node.id() || e.target().id() === node.id());
      const touchedEdges: EntityEdge[] = [];
      const neighbourIds = new Set<string>();
      incident.forEach((edge) => {
        const d = edge.data() as unknown as Record<string, unknown>;
        const other = edge.source().id() === node.id() ? edge.target() : edge.source();
        neighbourIds.add(other.id());
        touchedEdges.push({
          source: edge.source().id(),
          target: edge.target().id(),
          label: typeof d.label === "string" ? d.label : undefined,
          type: typeof d.edgeType === "string" ? d.edgeType : undefined,
          confidence: typeof d.confidence === "number" ? d.confidence : undefined,
        });
      });
      const neighbourhood = cy.nodes().filter((n) => neighbourIds.has(n.id()));
      const neighbours: EntityNode[] = [];
      neighbourhood.forEach((n) => {
        const d = n.data() as unknown as Record<string, unknown>;
        neighbours.push({
          id: n.id(),
          label: String(d.label ?? n.id()),
          type: normalizeEntityType(String(d.entityType ?? "other")),
          confidence: typeof d.confidence === "number" ? d.confidence : undefined,
        });
      });
      const relatedSignals = signalsRef.current.filter((signal) => {
        const text = `${signal.type} ${JSON.stringify(signal.value ?? "")}`.toLowerCase();
        return text.includes(node.id().toLowerCase()) || text.includes(typed.label.toLowerCase());
      });
      return { node: typed, edges: touchedEdges, neighbours, signals: relatedSignals };
    };

    const onTapNode = (event: EventObjectNode) => {
      const selection = describe(event.target);
      onSelectRef.current?.(selection);
      highlightNeighbourhood(event.target, cy);
    };
    const onTapEdge = (event: EventObjectEdge) => {
      const edge = event.target;
      onSelectRef.current?.({
        node: {
          id: edge.source().id(),
          label: edge.source().data("label") ?? edge.source().id(),
          type: normalizeEntityType(String(edge.source().data("entityType") ?? "other")),
        },
        edges: [
          {
            source: edge.source().id(),
            target: edge.target().id(),
            label: String(edge.data("label") ?? ""),
          },
        ],
        neighbours: [],
        signals: [],
      });
    };
    const onTapBackground = () => {
      onSelectRef.current?.(null);
      cy.elements().removeClass("dimmed neighbour");
    };

    cy.on("tap", "node", onTapNode);
    cy.on("tap", "edge", onTapEdge);
    cy.on("tap", onTapBackground);

    // Double-click expands the 2-hop neighbourhood.
    cy.on("dblclick", "node", (event) => {
      const node = event.target;
      const twoHop = node.neighborhood("node").nodes();
      const ids = twoHop.map((n: NodeSingular) => n.id());
      highlightNeighbourhood(node, cy, ids);
    });

    return () => {
      cy.off("tap", "node", onTapNode);
      cy.off("tap", "edge", onTapEdge);
      cy.off("tap", onTapBackground);
      cy.destroy();
      cyRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges, clusters, entityById]);

  /* ── Layout ── */
  const runLayout = useCallback(
    async (name: GraphLayout, instance: Core) => {
      const nodes = instance.nodes().length;
      if (nodes === 0) return;
      if (name === "cose-bilkent") {
        const ok = await ensureCoseBilkent();
        if (!ok) {
          onLayoutChange?.("cose");
          layoutRef.current = "cose";
        }
      }
      const effective = coseBilkentReady ? "cose-bilkent" : "cose";

      const base = {
        name: effective,
        animate: nodes < 400,
        animationDuration: 400,
        padding: 48,
      };

      if (effective === "cose-bilkent") {
        const options: CoseBilkentLayout = {
          ...base,
          name: "cose-bilkent",
          nodeRepulsion: () => 12000,
          idealEdgeLength: () => 130,
          edgeElasticity: () => 160,
          nestingFactor: () => 1.2,
          gravity: 0.35,
          numIter: 1200,
          tile: true,
        };
        instance.layout(options as unknown as cytoscape.LayoutOptions).run();
        return;
      }

      switch (name) {
        case "circle":
          instance.layout({ name: "circle", padding: 48, animate: nodes < 400 }).run();
          break;
        case "concentric":
          instance
            .layout({
              name: "concentric",
              padding: 48,
              minNodeSpacing: 20,
              concentric: (node: NodeSingular) => Number(node.data("weight") ?? 1),
              levelWidth: () => 2,
            })
            .run();
          break;
        case "grid":
          instance.layout({ name: "grid", padding: 48, avoidOverlap: true }).run();
          break;
        case "breadthfirst":
          instance
            .layout({
              name: "breadthfirst",
              directed: true,
              padding: 48,
              spacingFactor: 1.2,
            })
            .run();
          break;
        case "cose":
        default:
          instance
            .layout({
              ...base,
              name: "cose",
              nodeRepulsion: () => 9000,
              idealEdgeLength: () => 120,
              edgeElasticity: () => 150,
              gravity: 0.4,
              numIter: 1000,
              initialTemp: 200,
              coolingFactor: 0.95,
              minTemp: 1,
            })
            .run();
          break;
      }
    },
    [onLayoutChange],
  );

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !ready) return;
    void runLayout(layout, cy);
    const timer = window.setTimeout(() => cy.fit(undefined, 48), 450);
    return () => window.clearTimeout(timer);
  }, [layout, ready, runLayout]);

  /* ── Type visibility ── */
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      for (const type of ENTITY_TYPES) {
        const show = visibleTypes.has(type);
        cy.nodes(`[entityType = "${type}"]`).toggleClass("hidden-by-filter", !show);
      }
      // Hide edges whose endpoints are both filtered out.
      cy.edges().forEach((edge) => {
        const s = edge.source();
        const t = edge.target();
        const hide = s.hasClass("hidden-by-filter") && t.hasClass("hidden-by-filter");
        edge.toggleClass("hidden-by-filter", hide);
      });
    });
  }, [visibleTypes]);

  const toggleType = useCallback((type: EntityType) => {
    setVisibleTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }, []);

  const showAllTypes = useCallback(() => setVisibleTypes(new Set(ENTITY_TYPES)), []);

  /* ── Search within graph ── */
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const needle = query.trim().toLowerCase();
    cy.batch(() => {
      if (needle.length === 0) {
        cy.elements().removeClass("dimmed match neighbour");
        setMatchCount(0);
        return;
      }
      let count = 0;
      cy.nodes().forEach((node) => {
        if (String(node.data("isCluster") ?? "") === "true") return;
        const label = String(node.data("label") ?? "").toLowerCase();
        const id = node.id().toLowerCase();
        const hit = label.includes(needle) || id.includes(needle);
        node.toggleClass("match", hit);
        node.toggleClass("dimmed", !hit);
        if (hit) count += 1;
      });
      cy.edges().forEach((edge) => {
        const dim = edge.source().hasClass("dimmed") && edge.target().hasClass("dimmed");
        edge.toggleClass("dimmed", dim);
      });
      setMatchCount(count);
    });
  }, [query, ready]);

  const focusFirstMatch = useCallback(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const match = cy.nodes(".match").first();
    if (match.empty()) return;
    cy.animate({ center: { eles: match }, zoom: 1.6 }, { duration: 240 });
  }, []);

  /* ── 2-hop expansion ── */
  const expandSelected = useCallback(async () => {
    const cy = cyRef.current;
    const selected = cy?.$("node:selected");
    if (!cy || !selected || selected.empty()) return;
    const nodeId = selected.first().id();
    if (!onExpandRef.current) return;
    setExpanding(true);
    try {
      const added = await onExpandRef.current(nodeId);
      if (added && (added.nodes.length > 0 || added.edges.length > 0)) {
        onMergeRef.current?.(added);
        const known = new Set(cy.nodes().map((n) => n.id()));
        cy.batch(() => {
          const newNodes = toNodeElements(added.nodes).filter((n) => {
            const id = String((n.data as Record<string, unknown>).id);
            return !known.has(id);
          });
          if (newNodes.length > 0) cy.add(newNodes);
          const newIds = new Set(cy.nodes().map((n) => n.id()));
          const newEdges = toEdgeElements(added.edges, newIds);
          if (newEdges.length > 0) cy.add(newEdges);
        });
        cy.fit(undefined, 60);
      }
    } finally {
      setExpanding(false);
    }
  }, []);

  /* ── Imperative API ── */
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !ready || !onReady) return;
    onReady({
      toPng: (opts) => {
        try {
          return cy.png({ full: true, scale: opts?.scale ?? 1, bg: "#0d1117" });
        } catch {
          return null;
        }
      },
      toJSON: () => ({
        nodes: cy.nodes().map((node) => {
          const d = node.data() as unknown as Record<string, unknown>;
          return {
            id: node.id(),
            label: String(d.label ?? node.id()),
            type: normalizeEntityType(String(d.entityType ?? "other")),
            weight: typeof d.weight === "number" ? d.weight : undefined,
            confidence: typeof d.confidence === "number" ? d.confidence : undefined,
            group: typeof d.cluster === "string" ? d.cluster : undefined,
          } satisfies GraphNode;
        }),
        edges: cy.edges().map((edge) => {
          const d = edge.data() as unknown as Record<string, unknown>;
          return {
            source: edge.source().id(),
            target: edge.target().id(),
            label: typeof d.label === "string" ? d.label : undefined,
            type: typeof d.edgeType === "string" ? d.edgeType : undefined,
            confidence: typeof d.confidence === "number" ? d.confidence : undefined,
          } satisfies GraphEdge;
        }),
      }),
      fit: () => cy.fit(undefined, 48),
      focusNode: (id) => {
        const node = cy.$id(id);
        if (node.empty()) return false;
        cy.nodes().unselect();
        node.select();
        cy.animate({ center: { eles: node }, zoom: Math.max(cy.zoom(), 1.4) }, { duration: 300 });
        highlightNeighbourhood(node, cy);
        const selection = describePublic(node, cy, entityById, signals);
        if (selection) onSelectRef.current?.(selection);
        return true;
      },
      relayout: () => void runLayout(layoutRef.current, cy),
      stats: () => ({
        nodes: cy.nodes().length,
        edges: cy.edges().length,
        hidden: cy.nodes(".hidden-by-filter").length,
      }),
    });
  }, [ready, onReady, entityById, signals, runLayout]);

  /* ── Zoom controls ── */
  const zoomBy = useCallback((factor: number) => {
    const cy = cyRef.current;
    const el = containerRef.current;
    if (!cy || !el) return;
    cy.zoom({
      level: Math.min(4, Math.max(0.05, cy.zoom() * factor)),
      renderedPosition: { x: el.clientWidth / 2, y: el.clientHeight / 2 },
    });
  }, []);

  const hasData = nodes.length > 0;
  const typeCounts = useMemo(() => {
    const counts = new Map<EntityType, number>();
    for (const node of nodes) {
      const type = normalizeEntityType(node.type);
      counts.set(type, (counts.get(type) ?? 0) + 1);
    }
    return counts;
  }, [nodes]);

  const LAYOUT_OPTIONS: readonly { value: GraphLayout; label: string }[] = [
    { value: "cose", label: "Force" },
    { value: "cose-bilkent", label: "Bilkent" },
    { value: "circle", label: "Circle" },
    { value: "concentric", label: "Weight" },
    { value: "grid", label: "Grid" },
    { value: "breadthfirst", label: "Tree" },
  ];

  return (
    <div className={clsx("relative flex min-h-0 min-w-0 flex-col", className)}>
      {/* Toolbar */}
      {(toolbar || hasData) && (
        <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-1 px-2 py-1.5">
          <div className="relative flex min-w-[180px] flex-1 items-center">
            <TextInput
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  focusFirstMatch();
                }
              }}
              placeholder="Search within graph…"
              aria-label="Search within the graph"
              className="!py-1 !text-[11px]"
            />
            {query && (
              <>
                <Badge tone="info" className="ml-1.5">
                  {matchCount} hit{matchCount === 1 ? "" : "s"}
                </Badge>
                <Button
                  size="sm"
                  variant="ghost"
                  iconLabel="Clear graph search"
                  onClick={() => setQuery("")}
                >
                  ✕
                </Button>
              </>
            )}
          </div>

          <Segmented
            label="Graph layout"
            value={layout}
            onChange={(next) => onLayoutChange?.(next)}
            options={LAYOUT_OPTIONS}
          />

          <div className="flex items-center gap-1">
            <Tooltip label="Zoom in">
              <Button size="sm" variant="outline" iconLabel="Zoom in" onClick={() => zoomBy(1.25)}>
                +
              </Button>
            </Tooltip>
            <Tooltip label="Zoom out">
              <Button size="sm" variant="outline" iconLabel="Zoom out" onClick={() => zoomBy(0.8)}>
                −
              </Button>
            </Tooltip>
            <Tooltip label="Fit to view">
              <Button
                size="sm"
                variant="outline"
                iconLabel="Fit graph to view"
                onClick={() => cyRef.current?.fit(undefined, 48)}
              >
                ⊙
              </Button>
            </Tooltip>
            {onExpandNeighbourhood && (
              <Button
                size="sm"
                variant="outline"
                loading={expanding}
                onClick={() => void expandSelected()}
                title="Expand two hops from the selected node"
              >
                2-hop
              </Button>
            )}
          </div>

          {toolbar}
        </div>
      )}

      {/* Canvas */}
      <div className="relative min-h-0 flex-1">
        <div
          ref={containerRef}
          role="application"
          aria-label={ariaLabel}
          tabIndex={0}
          className="hairline-grid-dense h-full w-full rounded-b-md bg-surface-1"
        />

        {!hasData && (
          <div className="absolute inset-0 flex items-center justify-center">
            <EmptyState
              glyph="∞"
              title="NO ENTITIES DETECTED"
              description="Run an investigation to build the entity graph."
            />
          </div>
        )}

        {/* Legend — toggles visibility, doubles as a filter bar */}
        {!hideLegend && hasData && (
          <div className="pointer-events-none absolute left-2 top-2 max-h-[calc(100%-2rem)] overflow-y-auto">
            <Panel
              title="Entity types"
              eyebrow="LEGEND"
              bodyClassName="!p-1.5"
              className="pointer-events-auto bg-surface-1/95 backdrop-blur-sm"
            >
              <ul className="flex flex-col gap-1">
                {ENTITY_GROUP_ORDER.map((group: EntityGroup) => {
                  const types = typesInGroup(group).filter((t) => (typeCounts.get(t) ?? 0) > 0);
                  if (types.length === 0) return null;
                  return (
                    <li key={group} className="flex flex-col gap-0.5">
                      <span className="mono-label !text-[8px]">{group}</span>
                      <div className="flex flex-wrap gap-1">
                        {types.map((type) => (
                          <LegendToggle
                            key={type}
                            type={type}
                            count={typeCounts.get(type) ?? 0}
                            active={visibleTypes.has(type)}
                            onToggle={() => toggleType(type)}
                          />
                        ))}
                      </div>
                    </li>
                  );
                })}
              </ul>
              {visibleTypes.size < ENTITY_TYPES.length && (
                <Button size="sm" variant="ghost" onClick={showAllTypes} className="mt-1.5">
                  Show all
                </Button>
              )}
            </Panel>
          </div>
        )}

        {/* Counts */}
        {hasData && (
          <div className="pointer-events-none absolute bottom-2 left-2 flex items-center gap-2">
            <Badge tone="idle">
              {nodes.length} NODES · {edges.length} EDGES
            </Badge>
            {nodes.length > LABEL_BUDGET && (
              <Badge tone="warn" title="Labels hidden above 120 nodes for performance">
                LABELS HIDDEN
              </Badge>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Legend chip ────────────────────────────────────────────────────────────── */

function LegendToggle({
  type,
  count,
  active,
  onToggle,
}: {
  type: EntityType;
  count: number;
  active: boolean;
  onToggle: () => void;
}) {
  const color = colorFor(type);
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onToggle}
      title={`${labelFor(type)} — ${count}. Click to ${active ? "hide" : "show"}`}
      className={clsx(
        "focus-ring inline-flex items-center gap-1 rounded border px-1 py-[1px] font-mono text-[9px] uppercase tracking-[0.08em] transition-opacity",
        !active && "border-border-subtle text-ink-faint line-through opacity-60",
      )}
      style={
        active
          ? { color, background: `${color}17`, borderColor: `${color}4d` }
          : undefined
      }
    >
      <span
        aria-hidden="true"
        className="inline-block h-[7px] w-[7px] shrink-0 rounded-full"
        style={{ background: color }}
      />
      {labelFor(type)}
      <span className="tabular-nums opacity-70">{count}</span>
    </button>
  );
}

/* ── Selection helpers ──────────────────────────────────────────────────────── */

/** Dim everything except the node, its 1-hop (and optionally 2-hop) neighbours. */
function highlightNeighbourhood(
  node: NodeSingular,
  cy: Core,
  extraIds: readonly string[] = [],
): void {
  cy.batch(() => {
    cy.elements().removeClass("dimmed neighbour");
    const keep = new Set<string>([node.id(), ...extraIds]);
    node.closedNeighborhood().nodes().forEach((n) => {
      keep.add(n.id());
      n.addClass("neighbour");
    });
    for (const id of extraIds) {
      cy.$id(id).addClass("neighbour");
    }
    cy.nodes().forEach((n) => {
      if (!keep.has(n.id())) n.addClass("dimmed");
    });
    cy.edges().forEach((e) => {
      if (e.source().hasClass("dimmed") || e.target().hasClass("dimmed")) {
        e.addClass("dimmed");
      }
    });
  });
}

/** Build a {@link NodeSelection} outside the effect closure (for the API). */
function describePublic(
  node: NodeSingular,
  cy: Core,
  entityById: ReadonlyMap<string, Entity>,
  signals: readonly Signal[],
): NodeSelection | null {
  if (node.empty()) return null;
  const data = node.data() as unknown as Record<string, unknown>;
  const meta = entityById.get(node.id());
  const label = String(data.label ?? node.id());
  const neighbourIds = new Set<string>();
  const touchedEdges: EntityEdge[] = [];
  cy.edges()
    .filter((e) => e.source().id() === node.id() || e.target().id() === node.id())
    .forEach((edge) => {
      const d = edge.data() as unknown as Record<string, unknown>;
      const other = edge.source().id() === node.id() ? edge.target() : edge.source();
      neighbourIds.add(other.id());
      touchedEdges.push({
        source: edge.source().id(),
        target: edge.target().id(),
        label: typeof d.label === "string" ? d.label : undefined,
        type: typeof d.edgeType === "string" ? d.edgeType : undefined,
      });
    });
  const neighbours: EntityNode[] = [];
  cy.nodes()
    .filter((n) => neighbourIds.has(n.id()))
    .forEach((n) => {
      const d = n.data() as unknown as Record<string, unknown>;
      neighbours.push({
        id: n.id(),
        label: String(d.label ?? n.id()),
        type: normalizeEntityType(String(d.entityType ?? "other")),
        confidence: typeof d.confidence === "number" ? d.confidence : undefined,
      });
    });
  const haystack = `${node.id()} ${label}`.toLowerCase();
  return {
    node: {
      id: node.id(),
      label,
      type: normalizeEntityType(String(data.entityType ?? "other")),
      weight: typeof data.weight === "number" ? data.weight : undefined,
      confidence: typeof data.confidence === "number" ? data.confidence : undefined,
      group: typeof data.cluster === "string" ? data.cluster : undefined,
      attrs: meta?.attrs,
      lastSeen: meta?.last_seen,
    },
    edges: touchedEdges,
    neighbours,
    signals: signals.filter((signal) =>
      `${signal.type} ${JSON.stringify(signal.value ?? "")}`.toLowerCase().includes(haystack.split(" ")[0]),
    ),
  };
}

/* ── Helpers used by the GRAPH view ─────────────────────────────────────────── */

/** Detail text for one entity node, safe for the inspector panel. */
export function describeNode(node: EntityNode): string[] {
  const out = [truncate(node.label, 120)];
  if (node.attrs) {
    for (const [key, value] of Object.entries(node.attrs).slice(0, 6)) {
      out.push(`${key}: ${truncateMiddle(String(value), 48)}`);
    }
  }
  if (node.confidence !== undefined) out.push(`confidence ${confidencePct(node.confidence)}`);
  return out;
}

export { sizeStyle };
export type { Graph as GraphShape };
