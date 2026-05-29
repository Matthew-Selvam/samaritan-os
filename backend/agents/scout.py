"""
SCOUT — Search Agent
=====================
Runs dorks, federates across search engines, expands queries.
The first agent activated for almost every investigation.
"""
from __future__ import annotations

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

        # TODO: integrate SearXNG / SERP APIs here
        results = [
            {"engine": "stub", "query": dorks[0], "results": [], "note": "connect SearXNG for live results"},
        ]

        return AgentResult(
            agent=self.name,
            status="done",
            output={"dorks": dorks, "results": results, "engines_queried": ["stub"]},
            confidence=0.3,
            reasoning="Dork generation complete. Live search federation requires SearXNG connection.",
            latency_s=self._elapsed(t0),
        )
