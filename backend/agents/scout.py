"""
SCOUT — Search Agent
=====================
Runs dorks, federates across search engines, expands queries.
The first agent activated for almost every investigation.
"""
from __future__ import annotations

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

        # Live search via SearXNG (graceful degradation)
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
        except Exception as e:
            self.log(f"SearXNG unavailable — dorks only ({e})")

        live = bool(results)
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
            },
            confidence=0.8 if live else 0.3,
            reasoning=(
                f"Found {len(results)} results across {len(engines)} engines via SearXNG."
                if live else
                "Dork generation complete; SearXNG unavailable — live results pending."
            ),
            signals=[{"type": "dork", "query": d} for d in dorks],
            latency_s=self._elapsed(t0),
            error=search_data.get("error") if not live else None,
        )
