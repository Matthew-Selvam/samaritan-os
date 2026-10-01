"""
Sentinel.py — SENTINEL Agent
============================
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult

class SentinelAgent(BaseAgent):
    """Monitoring — baseline snapshots + change detection.

    Registers a target on a file-backed watchlist and snapshots its signal
    fingerprint. On a later run it diffs against the last snapshot and reports
    what changed (new signal types, count deltas) — the core of monitoring without
    a live event bus. A Redis pub/sub scheduler can drive re-runs later; the diff
    logic here is what it would call.
    """
    name = "SENTINEL"; role = "Live Monitoring"; icon = "⊲"
    description = "Real-time tracking: new posts, bio changes, username changes, new domains, leaks"
    preferred_models = ["phi3:mini"]; token_budget = 2048

    @staticmethod
    def _store_dir() -> str:
        import os
        default = ("/tmp/signal-os/sentinel_store" if os.getenv("VERCEL") else
                   os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "sentinel_store"))
        d = os.getenv("SENTINEL_DIR", default)
        os.makedirs(d, exist_ok=True)
        return d

    async def run(self, input_data, context=None):
        import os, json, hashlib
        from datetime import datetime, timezone
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        peer_signals = context.get("peer_signals", []) or []

        types = sorted({s.get("type", "?") for s in peer_signals})
        fingerprint = hashlib.sha256(
            ("|".join(types) + f"::{len(peer_signals)}").encode()
        ).hexdigest()[:16]
        key = hashlib.sha256(target.encode()).hexdigest()[:16]
        path = os.path.join(self._store_dir(), f"{key}.json")

        prior = None
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    prior = json.load(f)
            except Exception as e:
                self.log(f"snapshot read error: {e}")

        now = datetime.now(timezone.utc).isoformat()
        changes: list[str] = []
        if prior is None:
            self.log(f"new monitor registered for target (baseline: {len(peer_signals)} signals)")
        else:
            new_types = set(types) - set(prior.get("signal_types", []))
            gone_types = set(prior.get("signal_types", [])) - set(types)
            delta = len(peer_signals) - prior.get("signal_count", 0)
            if new_types:
                changes.append(f"new signal types: {', '.join(sorted(new_types))}")
            if gone_types:
                changes.append(f"dropped signal types: {', '.join(sorted(gone_types))}")
            if delta:
                changes.append(f"signal count {'+' if delta > 0 else ''}{delta}")
            self.log(f"diff vs {prior.get('last_seen', '?')[:10]}: "
                     + ("; ".join(changes) if changes else "no change"))

        snapshot = {"target": target[:120], "signal_count": len(peer_signals),
                    "signal_types": types, "fingerprint": fingerprint,
                    "first_seen": (prior or {}).get("first_seen", now), "last_seen": now,
                    "checks": (prior or {}).get("checks", 0) + 1}
        try:
            with open(path, "w") as f:
                json.dump(snapshot, f, indent=2)
        except Exception as e:
            self.log(f"snapshot write error: {e}")

        signals = [{"type": "monitor", "status": "baseline" if prior is None else "diffed",
                    "changes": changes, "checks": snapshot["checks"], "source": "sentinel"}]
        return AgentResult(
            agent=self.name, status="done",
            output={"monitoring": True, "baseline": prior is None,
                    "changes": changes, "snapshot": snapshot},
            confidence=0.6,
            reasoning=("Baseline snapshot registered." if prior is None
                       else (f"Detected {len(changes)} change(s)." if changes
                             else "No change since last check.")),
            signals=signals, latency_s=self._elapsed(t0),
        )
