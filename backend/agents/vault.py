"""
Vault.py — VAULT Agent
======================
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult

class VaultAgent(BaseAgent):
    """Memory — persistent entity store across investigations.

    File-backed JSON memory keyed by case (a Qdrant/Postgres backend can replace
    the store without changing the agent). On each run it loads what's already
    known for the case, merges the entities the swarm just produced, flags which
    are new vs. previously-seen, and persists the union. Runs in the correlation
    tier so it sees the full aggregated entity set.
    """
    name = "VAULT"; role = "Memory Agent"; icon = "□"
    description = "Persistent entity memory, semantic summarization, intelligence profile management"
    preferred_models = ["gemma2:9b"]; token_budget = 8192

    @staticmethod
    def _store_dir() -> str:
        import os
        default = ("/tmp/signal-os/vault_store" if os.getenv("VERCEL") else
                   os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "vault_store"))
        d = os.getenv("VAULT_DIR", default)
        os.makedirs(d, exist_ok=True)
        return d

    async def run(self, input_data, context=None):
        import os, json
        from datetime import datetime, timezone
        t0 = self._start_timer()
        context = context or {}
        case_id = context.get("case_id", "default")
        peer_entities = context.get("peer_entities", []) or []
        path = os.path.join(self._store_dir(), f"{case_id}.json")

        memory = {"case_id": case_id, "entities": {}, "first_seen": None, "runs": 0}
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    memory = json.load(f)
            except Exception as e:
                self.log(f"memory read error: {e}")

        known_ids = set(memory.get("entities", {}).keys())
        new_ids, seen_ids = [], []
        for e in peer_entities:
            eid = e.get("id")
            if not eid:
                continue
            if eid in known_ids:
                seen_ids.append(eid)
            else:
                new_ids.append(eid)
                memory["entities"][eid] = {"label": e.get("label"), "type": e.get("type")}

        now = datetime.now(timezone.utc).isoformat()
        memory["first_seen"] = memory.get("first_seen") or now
        memory["last_seen"] = now
        memory["runs"] = memory.get("runs", 0) + 1
        try:
            with open(path, "w") as f:
                json.dump(memory, f, indent=2)
        except Exception as e:
            self.log(f"memory write error: {e}")

        self.log(f"memory: {len(new_ids)} new, {len(seen_ids)} previously-seen, "
                 f"{len(memory['entities'])} total (run #{memory['runs']})")
        signals = [{"type": "memory", "new_entities": len(new_ids),
                    "known_entities": len(seen_ids), "total": len(memory["entities"]),
                    "run_count": memory["runs"], "source": "vault"}]
        if seen_ids:
            signals.append({"type": "recurrence", "entity_ids": seen_ids[:20],
                            "note": "entity seen in a prior investigation of this case",
                            "source": "vault"})
        return AgentResult(
            agent=self.name, status="done",
            output={"total_entities": len(memory["entities"]), "new": new_ids,
                    "previously_seen": seen_ids, "run_count": memory["runs"],
                    "store": path},
            confidence=0.9 if peer_entities else 0.5,
            reasoning=f"Memory for case {case_id}: {len(new_ids)} new entities, "
                      f"{len(seen_ids)} recurring, {len(memory['entities'])} total.",
            signals=signals, latency_s=self._elapsed(t0),
        )
