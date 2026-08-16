"""
reverse_image.py — Reverse Image Search Connector
===================================================
Google/Yandex/TinEye reverse image search, all through the OPSEC layer.
Feed it a URL or file path → get matching images from across the web.
"""
from __future__ import annotations

import base64
import os
from typing import Any
from urllib.parse import urlparse

from opsec import OpsecClient

SEARXNG_URL = os.getenv("SEARXNG_URL", "https://searx.be")


class ReverseImageConnector:
    """Multi-engine reverse image search through Tor."""

    def __init__(self) -> None:
        self._client = OpsecClient()

    async def search_by_url(self, image_url: str) -> dict[str, Any]:
        """Reverse image search via SearXNG image category."""
        try:
            resp = await self._client.get(
                f"{SEARXNG_URL.rstrip('/')}/search",
                params={
                    "q": f"!images {image_url}",
                    "format": "json",
                    "categories": "images",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for r in data.get("results", []):
                results.append({
                    "url": r.get("url"),
                    "title": r.get("title"),
                    "img_src": r.get("img_src"),
                    "source_engine": r.get("engine", "searxng"),
                    "similarity": None,  # SearXNG doesn't return similarity scores
                })
            return {"results": results, "count": len(results)}
        except Exception as e:
            return {"results": [], "count": 0, "error": str(e)}

    async def search_yandex(self, image_url: str) -> dict[str, Any]:
        """Yandex reverse image search — best engine for faces."""
        try:
            resp = await self._client.get(
                "https://yandex.com/images/search",
                params={"rpt": "imageview", "url": image_url},
            )
            # Yandex returns HTML — parse for similar image links
            # This is a basic extraction; a full parser would use BS4
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            results = []
            for item in soup.select("a.other-sites__snippet-link, a.CbirSites-ItemTitle"):
                href = item.get("href", "")
                title = item.get_text(strip=True)
                if href and title:
                    results.append({
                        "url": href,
                        "title": title,
                        "source_engine": "yandex",
                        "similarity": None,
                    })
            return {"results": results[:30], "count": len(results)}
        except Exception as e:
            return {"results": [], "count": 0, "error": str(e)}

    async def search_tineye(self, image_url: str) -> dict[str, Any]:
        """TinEye reverse search via their public endpoint."""
        try:
            resp = await self._client.post(
                "https://tineye.com/api/v1/result_json/",
                data={"url": image_url},
            )
            if resp.status_code != 200:
                return {"results": [], "count": 0, "error": f"TinEye HTTP {resp.status_code}"}
            data = resp.json()
            results = []
            for match in data.get("matches", []):
                for backlink in match.get("backlinks", []):
                    results.append({
                        "url": backlink.get("backlink"),
                        "title": backlink.get("source_name", ""),
                        "source_engine": "tineye",
                        "similarity": match.get("score"),
                    })
            return {"results": results, "count": len(results)}
        except Exception as e:
            return {"results": [], "count": 0, "error": str(e)}

    async def search_by_file(self, file_path: str) -> dict[str, Any]:
        """Reverse search from a local file — uploads to Yandex."""
        try:
            with open(file_path, "rb") as f:
                image_data = f.read()

            # Yandex image upload
            resp = await self._client.post(
                "https://yandex.com/images/search",
                files={"upfile": ("image.jpg", image_data, "image/jpeg")},
                params={"rpt": "imageview", "format": "json"},
            )
            # Try to extract redirect URL for results
            if resp.status_code in (200, 302):
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "html.parser")
                results = []
                for item in soup.select("a.other-sites__snippet-link, a.CbirSites-ItemTitle"):
                    href = item.get("href", "")
                    title = item.get_text(strip=True)
                    if href:
                        results.append({
                            "url": href,
                            "title": title,
                            "source_engine": "yandex_upload",
                            "similarity": None,
                        })
                return {"results": results[:30], "count": len(results)}
            return {"results": [], "count": 0, "error": f"Upload returned {resp.status_code}"}
        except Exception as e:
            return {"results": [], "count": 0, "error": str(e)}

    async def search_all(self, target: str, is_file: bool = False) -> dict[str, Any]:
        """Run all available engines concurrently."""
        import asyncio
        tasks = []
        if is_file:
            tasks.append(self.search_by_file(target))
        else:
            tasks.append(self.search_by_url(target))
            tasks.append(self.search_yandex(target))
            tasks.append(self.search_tineye(target))

        results_all: list[dict] = []
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, dict):
                results_all.extend(result.get("results", []))
        return {"results": results_all, "count": len(results_all)}

    async def close(self) -> None:
        await self._client.close()


async def run_reverse_image(target: str, **kwargs: Any) -> dict[str, Any]:
    """Top-level entry point. target = URL or file path."""
    connector = ReverseImageConnector()
    try:
        is_file = kwargs.get("is_file", os.path.isfile(target))
        return await connector.search_all(target, is_file=is_file)
    finally:
        await connector.close()
