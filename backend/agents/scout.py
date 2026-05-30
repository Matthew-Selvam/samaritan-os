"""
SCOUT — Search Agent
=====================
Runs dorks, federates across search engines, expands queries.
The first agent activated for almost every investigation.
"""
from __future__ import annotations

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

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

    async def run(self, input_data: str | dict, context: dict | None = None) -> AgentResult:
        t0 = self._start_timer()
        query = input_data if isinstance(input_data, str) else input_data.get("query", "")
        self.log(f"generating dorks for: {query[:60]}")

        dorks = [t.replace("{query}", query) for t in self.DORK_TEMPLATES]
        self.log(f"generated {len(dorks)} dork queries")
        self.log("search federation: Google + Bing + Yandex + DuckDuckGo (stub)")

        # Live Search Federation
        from connectors.searxng import run_searxng
        
        self.log(f"executing live search for: {query}")
        search_data = await run_searxng(query)
        
        results = search_data.get("results", [])
        engines = list(set([r.get("engine") for r in results if r.get("engine")]))

        return AgentResult(
            agent=self.name,
            status="done" if not search_data.get("error") else "partial",
            output={
                "dorks": dorks, 
                "results": results, 
                "engines_queried": engines or ["searxng"],
                "raw_search": search_data
            },
            confidence=0.8 if results else 0.3,
            reasoning=f"Found {len(results)} results across {len(engines)} engines via SearXNG." if results else "SearXNG returned no results or failed.",
            latency_s=self._elapsed(t0),
            error=search_data.get("error")
        )
