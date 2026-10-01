"""
PHONOS — Phone Number Intelligence
===================================
Given a phone number, PHONOS extracts everything derivable from public/offline
sources and emits it as Signal-OS signals + entities so the correlation tier
(NEXUS graph, KRONOS timeline, QUILL report) can fuse it with the rest of an
investigation.

Offline libphonenumber parsing always runs and carries the load:

* region, carrier, line type, timezones, normalized E.164 (from the connector)
* **time-zone inference** — the number's own zone, plus UTC offsets in effect
  right now, and the local wall-clock time the number would be "in"
* **numbering-plan portability** — whether the national number could be dialled
  abroad (leading zeros, trunk prefix, CC), and what a foreign dialer must add
* **country risk scoring** — weighted from VoIP/toll-free/premium line types,
  anonymity-proxy country tiers, and known high-risk number-plan traits
* **last-known-line-type heuristics** — carrier from the number-range registry,
  plus "has this block ever been reassigned" style signals

Live enrichment (NumVerify carrier validation, Dehashed breach exposure) runs only
when keys are configured and degrades to a logged note otherwise — the agent
always completes.

Scope: a consented / authorized COLLECTION agent. It profiles a number from
public metadata. It does not track live location, triangulate towers, or take
over accounts.
"""
from __future__ import annotations

import asyncio

from .base import BaseAgent, AgentResult


# ── Offline risk + portability tables (no key required) ──────────────────────

#: Carrier-risk weightings by line type. VoIP and premium ranges are the ones
#: most often used for burner accounts, SIM-swap and fraud, so they score high.
LINE_TYPE_RISK: dict[str, int] = {
    "premium_rate": 30, "voip": 25, "shared_cost": 20, "toll_free": 15,
    "personal_number": 10, "mobile": 8, "fixed_or_mobile": 5,
    "uan": 5, "fixed_line": 4, "pager": 8, "voicemail": 12, "unknown": 15,
}

#: Countries whose numbering plans are heavily used by anonymous-infrastructure
#: resellers. Weights are additive and deliberately coarse — this is a
#: prioritisation hint for a human reviewer, not an attribution claim.
COUNTRY_RISK: dict[str, int] = {
    "RU": 12, "IR": 12, "KP": 15, "SY": 12, "BY": 10, "VE": 10, "NG": 8,
    "ID": 7, "PK": 7, "BR": 6, "IN": 6, "CN": 6, "UA": 10, "AF": 12,
}

#: Countries with mature, strictly enforced national numbering plans.
PORTABLE_REGIONS = {"US", "CA", "GB", "DE", "FR", "JP", "AU", "NZ", "NL", "SE"}


class PhonosAgent(BaseAgent):
    name = "PHONOS"
    role = "Phone Intelligence"
    icon = "☏"
    description = ("Phone number workup: carrier, line type, region, timezone, "
                   "risk scoring, breach exposure, cross-reference pivots")
    preferred_models = ["qwen2.5:7b", "gemma2:9b"]
    token_budget = 6144

    # ── Offline analysis ─────────────────────────────────────────────────────

    @staticmethod
    def timezone_analysis(offline: dict) -> dict:
        """Describe the number's own time zone and the current local time.

        Args:
            offline: The connector's offline parse block.
        Returns:
            Dict with ``timezones``, ``primary_tz``, ``utc_offset``, ``dst_active``
            and ``local_time``; empty when the number has no zone metadata.
        """
        zones = offline.get("timezones") or []
        if not zones:
            return {"timezones": [], "resolved": False}
        try:
            from datetime import datetime, timezone as _tz
            from zoneinfo import ZoneInfo

            now = datetime.now(_tz.utc)
            primary = zones[0]
            info = ZoneInfo(primary)
            local = now.astimezone(info)
            offset = local.utcoffset()
            total_minutes = int(offset.total_seconds() // 60) if offset else 0
            dst = bool(local.dst())
            return {
                "timezones": list(zones),
                "primary_tz": primary,
                "utc_offset_minutes": total_minutes,
                "utc_offset": f"{total_minutes // 60:+03d}:{abs(total_minutes) % 60:02d}",
                "dst_active": dst,
                "local_time": local.isoformat(),
                "ambiguous_across_borders": len(set(zones)) > 1,
                "resolved": True,
            }
        except Exception:  # noqa: BLE001 — zoneinfo data may be absent
            return {"timezones": list(zones), "resolved": False}

    @staticmethod
    def portability(offline: dict) -> dict:
        """Report whether the number can be dialled from outside its country.

        Args:
            offline: The connector's offline parse block.
        Returns:
            Dict describing the international format and whether a foreign
            dialer must add or strip anything.
        """
        e164 = offline.get("e164") or ""
        intl = offline.get("international") or ""
        national = offline.get("national") or ""
        region = offline.get("region") or ""
        if not e164:
            return {"dialable_internationally": False, "reason": "no E.164 form"}

        country = e164.lstrip("+")[:2] if e164.startswith("+") else ""
        national_digits = "".join(c for c in national if c.isdigit())
        trunk = ""
        for candidate in ("0", "00"):
            if national_digits.startswith(candidate) and len(national_digits) > 6:
                trunk = candidate
                break
        return {
            "e164": e164,
            "international": intl,
            "national": national,
            "country_code": country,
            "region": region,
            "dialable_internationally": True,
            "trunk_prefix": trunk,
            "requires_trunk_stripping": bool(trunk),
            "mature_plan": region in PORTABLE_REGIONS,
            "note": (
                f"A foreign dialer must use {e164}; the national form "
                f"{national!r} is only valid inside {region or 'its country'}."
                if country else "No country calling code parsed."
            ),
        }

    @classmethod
    def risk_score(cls, offline: dict) -> dict:
        """Score a number's likelihood of being throwaway / fraud-linked.

        Args:
            offline: The connector's offline parse block.
        Returns:
            ``{"score", "band", "factors"}`` where score is 0–100.
        """
        factors: list[dict] = []
        line_type = (offline.get("line_type") or "unknown").lower()
        line_weight = LINE_TYPE_RISK.get(line_type, LINE_TYPE_RISK["unknown"])
        factors.append({"factor": f"line_type={line_type}", "weight": line_weight})

        region = (offline.get("region") or "").upper()
        country_weight = COUNTRY_RISK.get(region, 0)
        if country_weight:
            factors.append({"factor": f"region={region}", "weight": country_weight})

        if offline.get("carrier") in (None, "", "unknown"):
            factors.append({"factor": "no carrier metadata (spoofable/unassigned)",
                            "weight": 8})
        if not offline.get("valid"):
            factors.append({"factor": "number fails validity check", "weight": 20})
        if offline.get("possible") is False:
            factors.append({"factor": "number is not even possible", "weight": 15})

        score = min(100, sum(f["weight"] for f in factors))
        band = ("low" if score < 25 else "moderate" if score < 55
                else "high" if score < 80 else "severe")
        return {"score": score, "band": band, "factors": factors}

    @classmethod
    def last_known_line_type(cls, offline: dict) -> dict:
        """Best-effort "what was this block" summary from offline metadata.

        Args:
            offline: The connector's offline parse block.
        Returns:
            Dict with the carrier, the block-level type and a note on how much
            weight the answer deserves.
        """
        carrier = offline.get("carrier")
        line_type = offline.get("line_type")
        confidence = 0.9 if carrier else 0.4
        note = ("carrier resolved from the libphonenumber range database"
                if carrier else
                "no carrier for this range — the block is unallocated, a VoIP "
                "pool, or outside the metadata set")
        return {
            "carrier": carrier,
            "line_type": line_type,
            "confidence": confidence,
            "number_plan_region": offline.get("region"),
            "source": "libphonenumber",
            "note": note,
            "reassignment_risk": "unknown (needs a live numbering-portability feed)",
        }

    # ── Agent entry point ────────────────────────────────────────────────────

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
            if offline.get("carrier"):
                entities.append({
                    "id": f"carrier_{offline['carrier'].lower().replace(' ', '_')}",
                    "label": offline["carrier"],
                    "type": "carrier",
                })
                signals.append({"type": "carrier", "make": offline["carrier"],
                                "source": "libphonenumber"})
            if offline.get("location"):
                signals.append({"type": "phone_region", "region": offline.get("region"),
                                "location": offline.get("location"), "source": "libphonenumber"})
        else:
            self.log(f"number not valid ({offline.get('line_type', 'unparseable')})")

        # ── New offline analysis ──────────────────────────────────────────────
        timezones = self.timezone_analysis(offline)
        portability = self.portability(offline)
        risk = self.risk_score(offline)
        last_known = self.last_known_line_type(offline)

        if timezones.get("resolved"):
            self.log(f"timezone {timezones['primary_tz']} "
                     f"(UTC{timezones['utc_offset']}, local {timezones['local_time'][:16]})")
            signals.append({"type": "phone_timezone", **timezones, "source": "libphonenumber"})
        if portability.get("dialable_internationally"):
            self.log(f"international format {portability['e164']}"
                     + (" (strip the national trunk prefix when dialling abroad)"
                        if portability["requires_trunk_stripping"] else ""))
            signals.append({"type": "phone_portability", **portability,
                            "source": "libphonenumber"})
        self.log(f"risk {risk['score']}/100 ({risk['band']}) across "
                 f"{len(risk['factors'])} factor(s)")
        signals.append({"type": "phone_risk", **risk, "source": "phonos"})

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
                # Legacy keys — the frontend and stored reports read these.
                "offline": offline,
                "numverify": nv,
                "dehashed": dh,
                "pivots": pivots,
                "dorks": data.get("dorks", []),
                # New offline analysis.
                "timezones": timezones,
                "portability": portability,
                "risk": risk,
                "last_known_line_type": last_known,
            },
            confidence=confidence,
            reasoning=(
                f"Phone {e164}: valid={valid}, carrier={offline.get('carrier')}, "
                f"line={offline.get('line_type')}, region={offline.get('region')}, "
                f"tz={timezones.get('primary_tz') or 'unknown'}, "
                f"risk={risk['score']}/100 ({risk['band']}), "
                f"breach_records={dh.get('total', 0)}."
            ),
            signals=signals,
            entities_found=entities,
            latency_s=self._elapsed(t0),
        )


__all__ = ["PhonosAgent", "LINE_TYPE_RISK", "COUNTRY_RISK", "PORTABLE_REGIONS"]