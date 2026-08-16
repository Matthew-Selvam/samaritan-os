"""
PHONOS — Phone Number Intelligence
===================================
Given a phone number, PHONOS extracts everything derivable from public/offline
sources and emits it as Signal-OS signals + entities so the correlation tier
(NEXUS graph, KRONOS timeline, QUILL report) can fuse it with the rest of an
investigation.

Offline libphonenumber parsing always runs (region, carrier, line type,
timezones, normalized E.164). Live enrichment (NumVerify carrier validation,
Dehashed breach exposure) runs only when keys are configured, and degrades to a
logged note otherwise — the agent always completes.

Scope: a consented / authorized COLLECTION agent. It profiles a number from
public metadata. It does not track live location, triangulate towers, or take
over accounts.
"""
from __future__ import annotations

import asyncio

from .base import BaseAgent, AgentResult


class PhonosAgent(BaseAgent):
    name = "PHONOS"
    role = "Phone Intelligence"
    icon = "☏"
    description = ("Phone number workup: carrier, line type, region, timezone, "
                   "breach exposure, cross-reference pivots")
    preferred_models = ["qwen2.5:7b", "gemma2:9b"]
    token_budget = 6144

    async def run(self, input_data, context=None) -> AgentResult:
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        region = context.get("region")

        signals: list[dict] = []
        entities: list[dict] = []

        try:
            from connectors.phone_intel import run_phone_intel
            self.log(f"phone workup: {target}")
            data = await asyncio.wait_for(run_phone_intel(target, default_region=region),
                                          timeout=40.0)
        except asyncio.TimeoutError:
            self.log("phone intel timed out after 40s")
            return AgentResult(agent=self.name, status="error", output=None,
                               error="timeout", latency_s=self._elapsed(t0))
        except Exception as e:  # noqa: BLE001 — surface as a clean result
            self.log(f"phone intel error: {e}")
            return AgentResult(agent=self.name, status="error", output=None,
                               error=str(e), latency_s=self._elapsed(t0))

        offline = data.get("offline", {})
        if offline.get("error"):
            self.log(f"parse: {offline['error']}")
        valid = offline.get("valid", False)

        # ── Core number entity + signal ───────────────────────────────────────
        e164 = offline.get("e164") or target
        if valid:
            self.log(f"valid — {offline.get('carrier') or 'unknown carrier'}, "
                     f"{offline.get('line_type')}, {offline.get('region')} "
                     f"({offline.get('location') or 'no locale'})")
            entities.append({
                "id": f"phone_{''.join(c for c in e164 if c.isdigit())}",
                "label": e164,
                "type": "phone",
            })
            signals.append({
                "type": "phone_profile",
                "e164": e164,
                "carrier": offline.get("carrier"),
                "line_type": offline.get("line_type"),
                "region": offline.get("region"),
                "location": offline.get("location"),
                "timezones": offline.get("timezones"),
                "country_code": offline.get("country_code"),
                "source": "libphonenumber",
            })
            # Carrier as its own entity for graph clustering.
            if offline.get("carrier"):
                entities.append({
                    "id": f"carrier_{offline['carrier'].lower().replace(' ', '_')}",
                    "label": offline["carrier"],
                    "type": "carrier",
                })
                signals.append({"type": "carrier", "make": offline["carrier"],
                                "source": "libphonenumber"})
            # Coarse geo signal (region-level, from number plan — NOT live location).
            if offline.get("location"):
                signals.append({"type": "phone_region", "region": offline.get("region"),
                                "location": offline.get("location"), "source": "libphonenumber"})
        else:
            self.log(f"number not valid ({offline.get('line_type', 'unparseable')})")

        # ── Pivots / dorks for downstream cross-referencing ───────────────────
        pivots = data.get("pivots", [])
        if pivots:
            self.log(f"{len(pivots)} cross-reference pivot(s) generated")
            signals.append({"type": "pivots", "values": pivots, "source": "phonos"})

        # ── Live: NumVerify ───────────────────────────────────────────────────
        nv = data.get("numverify", {})
        if nv.get("error"):
            self.log(f"NumVerify: {nv['error']}")
        elif nv:
            self.log(f"NumVerify carrier: {nv.get('carrier')} ({nv.get('line_type')})")
            signals.append({"type": "carrier_live", "carrier": nv.get("carrier"),
                            "line_type": nv.get("line_type"), "country": nv.get("country"),
                            "source": "numverify"})

        # ── Live: Dehashed breach exposure ────────────────────────────────────
        dh = data.get("dehashed", {})
        if dh.get("error"):
            self.log(f"Dehashed: {dh['error']}")
        elif dh.get("total"):
            self.log(f"Dehashed: exposed in {dh['total']} record(s)")
            for b in dh.get("breaches", []):
                signals.append({"type": "breach", "name": b.get("database"),
                                "email": b.get("email"), "username": b.get("username"),
                                "source": "dehashed"})
                if b.get("email"):
                    entities.append({"id": f"email_{b['email'].lower()}",
                                     "label": b["email"], "type": "email"})
                if b.get("username"):
                    entities.append({"id": f"username_{b['username'].lower()}",
                                     "label": b["username"], "type": "username"})

        live_hits = bool(nv.get("carrier") or dh.get("total"))
        confidence = 0.85 if (valid and live_hits) else (0.6 if valid else 0.15)
        return AgentResult(
            agent=self.name, status="done",
            output={
                "offline": offline,
                "numverify": nv,
                "dehashed": dh,
                "pivots": pivots,
                "dorks": data.get("dorks", []),
            },
            confidence=confidence,
            reasoning=(
                f"Phone {e164}: valid={valid}, carrier={offline.get('carrier')}, "
                f"line={offline.get('line_type')}, region={offline.get('region')}, "
                f"breach_records={dh.get('total', 0)}."
            ),
            signals=signals,
            entities_found=entities,
            latency_s=self._elapsed(t0),
        )
