"""
SCOUT — Search Agent
=====================
Runs dorks, federates across search engines, expands queries.
The first agent activated for almost every investigation.

Alongside (instant) dork generation, SCOUT dispatches three OSINT connectors
concurrently: SearXNG (live meta-search), theHarvester (emails/subdomains/hosts),
and SpiderFoot (automated OSINT scan). Every connector degrades gracefully — a
missing binary, key, or unreachable service yields an {"error": ...} result, never
an exception — so SCOUT always completes.
"""
from __future__ import annotations

import asyncio
import os

from .base import BaseAgent, AgentResult


class ScoutAgent(BaseAgent):
    name = "SCOUT"
    role = "Search Intelligence"
    icon = "◎"
    description = "Runs dorks, search federation, query expansion across Google/Bing/Yandex/DuckDuckGo"
    preferred_models = ["qwen2.5:7b", "gemma2:9b"]
    context_folders = ["research", "osint"]
    token_budget = 4096

    DORK_TEMPLATES = [
        'site:linkedin.com "{query}"',
        'site:github.com "{query}"',
        '"{query}" filetype:pdf',
        '"{query}" site:reddit.com',
        'intitle:"{query}" -site:facebook.com',
        '"{query}" email OR contact',
    ]

    SEARXNG_URL = os.getenv("SEARXNG_URL", "https://searx.be")

    async def run(self, input_data: str | dict, context: dict | None = None) -> AgentResult:
        t0 = self._start_timer()
        context = context or {}
        query = (input_data if isinstance(input_data, str) else input_data.get("query", "")).strip()

        if not query:
            self.log("no query provided")
            return AgentResult(
                agent=self.name, status="error", output={"dorks": []},
                error="empty query", latency_s=self._elapsed(t0),
            )

        self.log(f"generating dorks for: {query[:60]}")
        dorks = [t.replace("{query}", query) for t in self.DORK_TEMPLATES]
        self.log(f"generated {len(dorks)} dork queries")

        # External OSINT connectors — kicked off first so they run concurrently
        # with (CPU-bound, instant) dork generation and the SearXNG federation.
        self.log("dispatching theHarvester + SpiderFoot connectors in parallel")
        th_task = asyncio.ensure_future(self._theharvester(query))
        sf_task = asyncio.ensure_future(self._spiderfoot(query))

        # Live search via SearXNG (graceful degradation).
        search_data: dict = {}
        results: list = []
        engines: list = []
        try:
            from connectors.searxng import run_searxng
            self.log(f"federating via SearXNG ({self.SEARXNG_URL})…")
            search_data = await run_searxng(query, base_url=self.SEARXNG_URL)
            results = search_data.get("results", [])
            engines = list({r.get("engine") for r in results if r.get("engine")})
            self.log(f"got {len(results)} results from {len(engines)} engines")
        except Exception as e:  # noqa: BLE001
            self.log(f"SearXNG unavailable — dorks only ({e})")

        # Collect the concurrent connector results (bounded inside the helpers).
        th_res, sf_res = await asyncio.gather(th_task, sf_task)
        th_live = isinstance(th_res, dict) and "error" not in th_res
        sf_live = isinstance(sf_res, dict) and "error" not in sf_res
        self.log(f"theHarvester: {'live' if th_live else 'inactive'} — {self._note(th_res)}")
        self.log(f"SpiderFoot: {'live' if sf_live else 'inactive'} — {self._note(sf_res)}")

        signals = (
            [{"type": "dork", "query": d} for d in dorks]
            + self._th_signals(th_res)
            + self._sf_signals(sf_res)
        )
        live = bool(results) or th_live or sf_live
        return AgentResult(
            agent=self.name,
            status="done",
            output={
                "query": query,
                "dorks": dorks,
                "dork_count": len(dorks),
                "results": results,
                "engines_queried": engines or ["offline"],
                "live_search": live,
                "connectors": {"theharvester": th_res, "spiderfoot": sf_res},
            },
            confidence=0.8 if live else 0.3,
            reasoning=(
                f"Found {len(results)} SearXNG result(s); "
                f"theHarvester={'live' if th_live else 'off'}, "
                f"SpiderFoot={'live' if sf_live else 'off'}."
            ),
            signals=signals,
            latency_s=self._elapsed(t0),
            error=search_data.get("error") if not live else None,
        )

    # ── Connector helpers (lazy import + bounded, never raise) ─────────────────

    async def _theharvester(self, query: str) -> dict:
        """Bounded call to the theHarvester connector. Returns {"error": ...} on any failure."""
        try:
            from connectors.theharvester import run_theharvester
            return await asyncio.wait_for(run_theharvester(query), timeout=30.0)
        except asyncio.TimeoutError:
            return {"error": "theHarvester timed out after 30s"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"theHarvester: {e}"}

    async def _spiderfoot(self, query: str) -> dict:
        """Bounded call to the SpiderFoot connector. Caps the connector's internal poll
        (default 600s) and wraps it in a hard ceiling so the pipeline never stalls."""
        try:
            from connectors.spiderfoot import run_spiderfoot
            return await asyncio.wait_for(run_spiderfoot(query, timeout=8), timeout=12.0)
        except asyncio.TimeoutError:
            return {"error": "SpiderFoot timed out after 12s"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"SpiderFoot: {e}"}

    @staticmethod
    def _note(res: dict) -> str:
        if not isinstance(res, dict):
            return str(res)[:80]
        return res.get("error") or res.get("note") or res.get("status") or "ok"

    @staticmethod
    def _th_signals(res: dict) -> list[dict]:
        if not isinstance(res, dict) or "error" in res:
            return []
        sigs: list[dict] = []
        for e in (res.get("emails") or []):
            sigs.append({"type": "email", "value": e, "source": "theharvester"})
        for h in (res.get("hosts") or []):
            sigs.append({"type": "host", "value": h, "source": "theharvester"})
        for ip in (res.get("ips") or []):
            sigs.append({"type": "ip", "value": ip, "source": "theharvester"})
        return sigs

    @staticmethod
    def _sf_signals(res: dict) -> list[dict]:
        if not isinstance(res, dict) or "error" in res:
            return []
        sid = res.get("scan_id")
        return [{"type": "scan", "engine": "spiderfoot", "scan_id": sid,
                 "status": res.get("status")}] if sid else []
