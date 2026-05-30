"use client";

import Cytoscape from "cytoscape";
import { useEffect, useRef } from "react";

// ── Node type → colour ────────────────────────────────────────────────────────
const TYPE_COLOR: Record<string, string> = {
  person:        "#00d4ff",  // cyan
  domain:        "#00ff88",  // green
  ip:            "#ffb020",  // amber
  ip_address:    "#ffb020",
  email:         "#9966ff",  // purple
  wallet:        "#ff8833",  // orange
  crypto_wallet: "#ff8833",
  url:           "#44dd88",
  username:      "#ccddff",
  phone:         "#ffcc44",
};
const DEFAULT_COLOR = "#5a7a9a";

const LEGEND: [string, string][] = [
  ["PERSON", "#00d4ff"],
  ["DOMAIN", "#00ff88"],
  ["IP",     "#ffb020"],
  ["EMAIL",  "#9966ff"],
  ["WALLET", "#ff8833"],
];

// ── Exported types (consumed by page.tsx) ─────────────────────────────────────
export interface EntityNode {
  id: string;
  label: string;
  type: string;
}

export interface EntityEdge {
  source: string;
  target: string;
  label: string;
}

interface Props {
  nodes: EntityNode[];
  edges: EntityEdge[];
}

// ── Component ─────────────────────────────────────────────────────────────────
export function EntityGraph({ nodes, edges }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef        = useRef<Cytoscape.Core | null>(null);

  // Rebuild graph when data changes
  useEffect(() => {
    if (!containerRef.current) return;

    cyRef.current?.destroy();
    cyRef.current = null;

    if (nodes.length === 0) return;

    const cy: Cytoscape.Core = Cytoscape({
      container: containerRef.current,

      elements: [
        ...nodes.map((n) => ({
          data: {
            id:        n.id,
            label:     n.label,
            nodeColor: TYPE_COLOR[n.type] ?? DEFAULT_COLOR,
          },
        })),
        ...edges.map((e) => ({
          data: { source: e.source, target: e.target, label: e.label },
        })),
      ],

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      style: [
        {
          selector: "node",
          style: {
            "background-color":   "data(nodeColor)" as string,
            "background-opacity": 0.15,
            "border-width":       2,
            "border-color":       "data(nodeColor)" as string,
            "label":              "data(label)",
            "color":              "#c8d8e8",
            "font-size":          9,
            "font-family":        "JetBrains Mono, monospace",
            "text-valign":        "bottom",
            "text-halign":        "center",
            "text-margin-y":      4,
            "width":              28,
            "height":             28,
            "text-max-width":     "80px",
            "text-wrap":          "ellipsis" as "ellipsis",
          },
        },
        {
          selector: "edge",
          style: {
            "width":                    1,
            "line-color":               "#2a4060",
            "target-arrow-color":       "#2a4060",
            "target-arrow-shape":       "triangle" as "triangle",
            "curve-style":              "bezier" as "bezier",
            "label":                    "data(label)",
            "font-size":                8,
            "font-family":              "JetBrains Mono, monospace",
            "color":                    "#5a7a9a",
            "text-rotation":            "autorotate" as "autorotate",
            "text-background-color":    "#0d1117",
            "text-background-opacity":  0.9,
            "text-background-padding":  "2px",
          },
        },
        {
          selector: "node:selected",
          style: {
            "background-opacity": 0.35,
            "border-width":       3,
          },
        },
        {
          selector: "edge:selected",
          style: {
            "line-color":         "#00d4ff",
            "target-arrow-color": "#00d4ff",
          },
        },
      ],

      layout: {
        name:     "cose",
        animate:  true,
        animationDuration: 400,
        padding:  48,
        // cose tuning — keep nodes well-separated
        nodeRepulsion:  () => 9000,
        idealEdgeLength: () => 120,
        edgeElasticity:  () => 150,
        gravity:         0.4,
        numIter:         1000,
        initialTemp:     200,
        coolingFactor:   0.95,
        minTemp:         1,
      } as Parameters<Cytoscape.Core["layout"]>[0],

      userZoomingEnabled:  true,
      userPanningEnabled:  true,
      boxSelectionEnabled: false,
    });

    cyRef.current = cy;

    // Fit after layout completes
    cy.one("layoutstop", () => cy.fit(undefined, 48));
  }, [nodes, edges]);

  // Cleanup on unmount
  useEffect(() => () => { cyRef.current?.destroy(); }, []);

  // ── Zoom helpers ─────────────────────────────────────────────────────────────
  const zoomBy = (factor: number) => {
    if (!cyRef.current || !containerRef.current) return;
    const cy  = cyRef.current;
    const el  = containerRef.current;
    cy.zoom({
      level:            cy.zoom() * factor,
      renderedPosition: { x: el.clientWidth / 2, y: el.clientHeight / 2 },
    });
  };
  const fitAll = () => cyRef.current?.fit(undefined, 48);

  const ZOOM_BTNS = [
    { icon: "+",  fn: () => zoomBy(1.25), title: "Zoom in"  },
    { icon: "−",  fn: () => zoomBy(0.8),  title: "Zoom out" },
    { icon: "⊙", fn: fitAll,              title: "Fit graph"},
  ] as const;

  // ── Render ────────────────────────────────────────────────────────────────────
  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>

      {/* Cytoscape mount point — must always be in DOM so the ref attaches */}
      <div
        ref={containerRef}
        style={{
          width:        "100%",
          height:       "100%",
          background:   "var(--bg-card)",
          borderRadius: 6,
        }}
      />

      {/* ── Empty state overlay ── */}
      {nodes.length === 0 && (
        <div
          style={{
            position:       "absolute",
            inset:          0,
            display:        "flex",
            flexDirection:  "column",
            alignItems:     "center",
            justifyContent: "center",
            gap:            10,
            pointerEvents:  "none",
          }}
        >
          <div style={{ fontSize: 40, color: "var(--border-hi)", lineHeight: 1 }}>∞</div>
          <p style={{ color: "var(--text-muted)", fontSize: 12, margin: 0 }}>
            No entities detected yet.
          </p>
          <p style={{ color: "var(--text-muted)", fontSize: 10, margin: 0, opacity: 0.55 }}>
            Run an investigation to build the entity graph.
          </p>
        </div>
      )}

      {/* ── Controls (only when graph has data) ── */}
      {nodes.length > 0 && (
        <>
          {/* Zoom controls — bottom-right */}
          <div
            style={{
              position:      "absolute",
              bottom:        16,
              right:         16,
              display:       "flex",
              flexDirection: "column",
              gap:           4,
            }}
          >
            {ZOOM_BTNS.map((b) => (
              <button
                key={b.icon}
                onClick={b.fn}
                title={b.title}
                style={{
                  width:          28,
                  height:         28,
                  background:     "rgba(13,17,23,0.92)",
                  border:         "1px solid var(--border-hi)",
                  color:          "var(--text)",
                  borderRadius:   4,
                  cursor:         "pointer",
                  fontSize:       13,
                  display:        "flex",
                  alignItems:     "center",
                  justifyContent: "center",
                  fontFamily:     "var(--font-mono)",
                  transition:     "border-color 0.12s",
                }}
                onMouseEnter={(e) =>
                  (e.currentTarget.style.borderColor = "var(--cyan)")
                }
                onMouseLeave={(e) =>
                  (e.currentTarget.style.borderColor = "var(--border-hi)")
                }
              >
                {b.icon}
              </button>
            ))}
          </div>

          {/* Legend — top-left */}
          <div
            style={{
              position:      "absolute",
              top:           12,
              left:          12,
              background:    "rgba(13,17,23,0.88)",
              border:        "1px solid var(--border)",
              borderRadius:  4,
              padding:       "8px 10px",
              display:       "flex",
              flexDirection: "column",
              gap:           5,
              pointerEvents: "none",
            }}
          >
            {LEGEND.map(([label, color]) => (
              <div key={label} style={{ display: "flex", alignItems: "center", gap: 7 }}>
                <span
                  style={{
                    width:          8,
                    height:         8,
                    borderRadius:   "50%",
                    background:     color,
                    display:        "inline-block",
                    flexShrink:     0,
                    boxShadow:      `0 0 4px ${color}88`,
                  }}
                />
                <span
                  style={{
                    fontSize:       9,
                    color:          "var(--text-muted)",
                    letterSpacing:  "0.09em",
                  }}
                >
                  {label}
                </span>
              </div>
            ))}
          </div>

          {/* Node / edge count — bottom-left */}
          <div
            style={{
              position:      "absolute",
              bottom:        16,
              left:          12,
              fontSize:      9,
              color:         "var(--text-muted)",
              letterSpacing: "0.09em",
              pointerEvents: "none",
            }}
          >
            {nodes.length} NODES · {edges.length} EDGES
          </div>
        </>
      )}
    </div>
  );
}
