import httpx
import asyncio
from typing import List, Dict, Any

class SearXNGConnector:
    """
    Connector for SearXNG Search Engine.
    Supports self-hosted or public instances.
    """
    def __init__(self, base_url: str = "https://searx.be"):
        self.base_url = base_url.rstrip("/")

    async def search(self, query: str, categories: List[str] = None) -> Dict[str, Any]:
        params = {
            "q": query,
            "format": "json",
        }
        if categories:
            params["categories"] = ",".join(categories)

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(f"{self.base_url}/search", params=params)
                response.raise_for_status()
                data = response.json()
                
                results = []
                for res in data.get("results", []):
                    results.append({
                        "title": res.get("title"),
                        "url": res.get("url"),
                        "content": res.get("content"),
                        "engine": res.get("engine"),
                        "score": res.get("score")
                    })
                
                return {
                    "query": query,
                    "results": results,
                    "count": len(results)
                }
            except Exception as e:
                return {"error": str(e), "query": query, "results": []}

async def run_searxng(query: str, **kwargs) -> Dict[str, Any]:
    connector = SearXNGConnector(base_url=kwargs.get("base_url", "https://searx.be"))
    return await connector.search(query, categories=kwargs.get("categories"))
