"""
Nexus.py — NEXUS Agent
======================
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult

class NexusAgent(BaseAgent):
    """Correlation Engine — in-memory entity graph + relationship inference.

    Consumes the aggregated signals/entities of the primary swarm (injected by
    APEX as ``context['peer_signals']`` / ``context['peer_entities']``) and builds
    a de-duplicated knowledge graph: nodes, weighted edges, connected-component
    clusters, and degree-centrality influence scoring. Pure Python — no Neo4j
    required (a Neo4j sink can be layered on later without touching this logic).
    """
    name = "NEXUS"; role = "Correlation Engine"; icon = "∞"
    description = "Hidden relationship detection, graph clustering, influence scoring, semantic linking"
    preferred_models = ["gemma2:9b"]; token_budget = 12288

    @staticmethod
    def _slug(text: str) -> str:
        return "".join(c if c.isalnum() else "_" for c in str(text).lower()).strip("_") or "unknown"

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        peer_signals: list[dict] = context.get("peer_signals", []) or []
        peer_entities: list[dict] = context.get("peer_entities", []) or []

        self.log(f"correlating {len(peer_entities)} entities + {len(peer_signals)} signals")

        # ── Nodes: root target + de-duplicated peer entities ──────────────────
        root_id = f"target_{self._slug(target)}"
        nodes: dict[str, dict] = {
            root_id: {"id": root_id, "label": target[:60], "type": "target", "degree": 0}
        }
        for e in peer_entities:
            eid = e.get("id") or f"entity_{self._slug(e.get('label', ''))}"
            if eid not in nodes:
                nodes[eid] = {"id": eid, "label": e.get("label", eid),
                              "type": e.get("type", "entity"), "degree": 0}

        # ── Edges: connect every discovered entity back to the root target ────
        edges: list[dict] = []
        seen_edges: set[tuple] = set()

        def add_edge(a: str, b: str, relation: str, weight: float = 1.0, source: str = "nexus"):
            if a == b or a not in nodes or b not in nodes:
                return
            key = (a, b, relation)
            if key in seen_edges:
                return
            seen_edges.add(key)
            edges.append({"source": a, "target": b, "relation": relation,
                          "weight": weight, "provenance": source})
            nodes[a]["degree"] += 1
            nodes[b]["degree"] += 1

        for eid, node in list(nodes.items()):
            if eid == root_id:
                continue
            relation = {
                "username": "has_account", "person": "resolves_to",
                "location": "located_at",
            }.get(node["type"], "linked_to")
            add_edge(root_id, eid, relation)

        # ── Relationship inference from signals (co-location, breach, matches) ─
        gps_signals = [s for s in peer_signals if s.get("type") == "gps"]
        for i, g in enumerate(gps_signals):
            loc_id = f"loc_{round(g.get('lat', 0), 4)}_{round(g.get('lon', 0), 4)}"
            nodes.setdefault(loc_id, {"id": loc_id,
                                      "label": g.get("address") or f"{g.get('lat')}, {g.get('lon')}",
                                      "type": "location", "degree": 0})
            add_edge(root_id, loc_id, "located_at", weight=1.5, source="exif_gps")

        breach_count = sum(1 for s in peer_signals if s.get("type") == "breach")
        match_count = sum(1 for s in peer_signals if s.get("type") in ("face_match", "image_match"))
        account_count = sum(1 for s in peer_signals if s.get("type") == "account")

        # ── Connected-component clustering ────────────────────────────────────
        adjacency: dict[str, set[str]] = {n: set() for n in nodes}
        for e in edges:
            adjacency[e["source"]].add(e["target"])
            adjacency[e["target"]].add(e["source"])
        clusters: list[list[str]] = []
        unvisited = set(nodes)
        while unvisited:
            start = unvisited.pop()
            stack, comp = [start], [start]
            while stack:
                cur = stack.pop()
                for nb in adjacency[cur]:
                    if nb in unvisited:
                        unvisited.remove(nb)
                        stack.append(nb)
                        comp.append(nb)
            clusters.append(comp)

        # ── Influence scoring: normalized degree centrality ───────────────────
        max_deg = max((n["degree"] for n in nodes.values()), default=0) or 1
        ranked = sorted(nodes.values(), key=lambda n: n["degree"], reverse=True)
        for n in nodes.values():
            n["influence"] = round(n["degree"] / max_deg, 3)

        self.log(f"graph: {len(nodes)} nodes, {len(edges)} edges, "
                 f"{len(clusters)} cluster(s), top node '{ranked[0]['label']}'")

        signals = [{
            "type": "correlation_summary",
            "nodes": len(nodes), "edges": len(edges), "clusters": len(clusters),
            "accounts": account_count, "breaches": breach_count, "media_matches": match_count,
            "source": "nexus",
        }]
        density = len(edges) / max(len(nodes), 1)
        confidence = min(0.95, 0.3 + 0.1 * len(edges)) if edges else 0.1
        return AgentResult(
            agent=self.name, status="done",
            output={
                "nodes": list(nodes.values()),
                "edges": edges,
                "clusters": [{"size": len(c), "members": c} for c in clusters],
                "top_entities": [{"label": n["label"], "type": n["type"],
                                  "degree": n["degree"], "influence": n["influence"]}
                                 for n in ranked[:5]],
                "graph_density": round(density, 3),
            },
            confidence=round(confidence, 3),
            reasoning=f"Built graph of {len(nodes)} nodes / {len(edges)} edges across "
                      f"{len(clusters)} cluster(s); {account_count} accounts, "
                      f"{breach_count} breaches, {match_count} media matches correlated.",
            signals=signals,
            latency_s=self._elapsed(t0),
        )
