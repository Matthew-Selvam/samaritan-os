"""
Crawler.py — CRAWLER Agent
==========================
"""
from __future__ import annotations

import asyncio
from .base import BaseAgent, AgentResult

class CrawlerAgent(BaseAgent):
    """Web Scraper — static fetch + structured extraction.

    Fetches a URL/domain over the OPSEC layer (falls back to plain httpx) and
    extracts title, outbound links, emails, and phone numbers from the HTML. This
    is dependency-light static scraping; a Playwright browser-automation path can
    layer on later for JS-rendered targets without changing this contract.
    """
    name = "CRAWLER"; role = "Web Scraper"; icon = "⟨/⟩"
    description = "Structured data extraction, website parsing, browser automation (Playwright)"
    preferred_models = ["qwen2.5:7b"]; token_budget = 4096

    async def run(self, input_data, context=None):
        import re
        t0 = self._start_timer()
        target = input_data if isinstance(input_data, str) else str(input_data)
        url = target if target.startswith(("http://", "https://")) else f"https://{target}"
        signals: list[dict] = []
        entities: list[dict] = []

        html = ""
        status_code = None
        try:
            try:
                from opsec import OpsecClient
                client = OpsecClient(timeout=20.0)
                resp = await client.get(url)
            except Exception:
                import httpx
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
                    resp = await c.get(url, headers={"User-Agent": "Signal-OS-Crawler"})
            status_code = resp.status_code
            html = resp.text or ""
            self.log(f"fetched {url} — HTTP {status_code}, {len(html)} bytes")
        except Exception as e:
            self.log(f"fetch failed: {e}")
            return AgentResult(agent=self.name, status="error", output={"url": url},
                               error=str(e), latency_s=self._elapsed(t0))

        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = title_m.group(1).strip()[:200] if title_m else None
        links = sorted(set(re.findall(r'href=["\'](https?://[^"\'>\s]+)', html, re.I)))[:100]
        emails = sorted(set(re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", html)))
        phones = sorted(set(re.findall(r"\+?\d[\d\s().\-]{7,17}\d", html)))[:20]

        self.log(f"extracted: title={'yes' if title else 'no'}, {len(links)} links, "
                 f"{len(emails)} emails, {len(phones)} phones")
        for e in emails[:30]:
            signals.append({"type": "email_found", "value": e, "source": "crawler"})
            entities.append({"id": f"email_{e.lower()}", "label": e, "type": "email"})
        for p in phones:
            signals.append({"type": "phone_found", "value": p, "source": "crawler"})
        if links:
            signals.append({"type": "outbound_links", "count": len(links),
                            "sample": links[:10], "source": "crawler"})

        return AgentResult(
            agent=self.name, status="done",
            output={"url": url, "http_status": status_code, "title": title,
                    "links": links, "emails": emails, "phones": phones},
            confidence=0.7 if (status_code == 200 and (emails or links)) else 0.3,
            reasoning=f"Scraped {url}: {len(links)} links, {len(emails)} emails, "
                      f"{len(phones)} phone(s).",
            signals=signals, entities_found=entities,
            latency_s=self._elapsed(t0),
        )
