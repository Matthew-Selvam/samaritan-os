"""
Prism.py — PRISM Agent
======================
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult

class PrismAgent(BaseAgent):
    """Social Intelligence — full identity resolution pipeline.

    Sherlock username scan + people search + breach check + profile photo harvesting.
    """
    name = "PRISM"; role = "Social Intelligence"; icon = "◈"
    description = "Cross-platform identity resolution, account clustering, breach correlation"
    preferred_models = ["gemma2:9b"]; token_budget = 8192

    async def run(self, input_data, context=None):
        t0 = self._start_timer()
        context = context or {}
        target = input_data if isinstance(input_data, str) else str(input_data)
        input_type = context.get("input_type", "username")
        signals: list[dict] = []
        entities: list[dict] = []

        # ── Sherlock username scan ────────────────────────────────────────
        sherlock_data = {}
        try:
            from connectors.sherlock import run_sherlock
            self.log(f"Sherlock username scan: {target}")
            sherlock_data = await asyncio.wait_for(run_sherlock(target), timeout=30.0)
            found = sherlock_data.get("count", 0)
            self.log(f"found {found} accounts across platforms")
            for r in sherlock_data.get("results", []):
                signals.append({
                    "type": "account", "platform": r.get("site"),
                    "url": r.get("url"), "source": "sherlock",
                })
                entities.append({
                    "id": f"account_{r.get('site', '').lower()}_{target}",
                    "label": f"{target}@{r.get('site')}",
                    "type": "username",
                })
        except FileNotFoundError:
            self.log("sherlock not installed — run: pip install sherlock-project")
        except asyncio.TimeoutError:
            self.log("Sherlock timed out after 30s")
        except Exception as e:
            self.log(f"Sherlock error: {e}")

        # ── People search (for name inputs) ───────────────────────────────
        people_data = {}
        if input_type == "person_name":
            try:
                from connectors.people_search import run_people_search
                self.log(f"people search: {target}")
                people_data = await asyncio.wait_for(
                    run_people_search(target, location=context.get("location")),
                    timeout=25.0,
                )
                count = people_data.get("count", 0)
                self.log(f"found {count} people records")
                for r in people_data.get("results", []):
                    signals.append({
                        "type": "person_record", "source": r.get("source"),
                        "full_name": r.get("full_name"), "age": r.get("age"),
                    })
                    entities.append({
                        "id": f"person_{r.get('full_name', '').replace(' ', '_').lower()}",
                        "label": r.get("full_name", target),
                        "type": "person",
                    })
            except Exception as e:
                self.log(f"People search error: {e}")

        # ── Breach check (for emails) ─────────────────────────────────────
        breach_data = {}
        if input_type in ("email", "username"):
            try:
                from connectors.breach_check import run_breach_check
                self.log(f"breach check: {target}")
                breach_data = await asyncio.wait_for(
                    run_breach_check(target), timeout=15.0,
                )
                breaches = breach_data.get("total_breaches", 0)
                self.log(f"found {breaches} breach(es)")
                for b in breach_data.get("breaches", []):
                    signals.append({
                        "type": "breach", "name": b.get("name"),
                        "date": b.get("date"), "source": "hibp",
                    })
            except Exception as e:
                self.log(f"Breach check error: {e}")

        has_live = bool(sherlock_data.get("count") or people_data.get("count") or breach_data.get("total_breaches"))
        return AgentResult(
            agent=self.name, status="done",
            output={
                "sherlock": sherlock_data,
                "people_search": people_data,
                "breach_check": breach_data,
                "input_type": input_type,
            },
            confidence=0.85 if has_live else 0.3,
            reasoning=f"Identity resolution: Sherlock={sherlock_data.get('count', 0)} accounts, "
                      f"people={people_data.get('count', 0)}, breaches={breach_data.get('total_breaches', 0)}",
            signals=signals,
            entities_found=entities,
            latency_s=self._elapsed(t0),
        )
