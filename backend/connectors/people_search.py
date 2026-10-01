"""
people_search.py — Public People Search Connector
===================================================
Scrapes public people-search engines through Tor for name-based deanonymization.
Extracts: names, ages, addresses, phones, emails, relatives.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote_plus

from opsec import OpsecClient


class PeopleSearchConnector:
    """Scrape public people search engines through Tor."""

    def __init__(self) -> None:
        self._client = OpsecClient(timeout=20.0)

    async def search_thatsthem(self, name: str, location: str | None = None) -> dict[str, Any]:
        """Search ThatsThem people search."""
        try:
            slug = quote_plus(name.replace(" ", "-").lower())
            url = f"https://thatsthem.com/name/{slug}"
            if location:
                url += f"/{quote_plus(location.replace(' ', '-').lower())}"

            resp = await self._client.get(url)
            if resp.status_code != 200:
                return {"source": "thatsthem", "results": [], "error": f"HTTP {resp.status_code}"}

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            results = []

            for card in soup.select(".ThatsThem-people-record, .result-card"):
                person: dict[str, Any] = {"source": "thatsthem"}
                # Name
                name_el = card.select_one("h2, .name")
                if name_el:
                    person["full_name"] = name_el.get_text(strip=True)
                # Age
                age_el = card.select_one(".age, .ThatsThem-age")
                if age_el:
                    age_text = age_el.get_text(strip=True)
                    age_match = re.search(r"(\d+)", age_text)
                    person["age"] = int(age_match.group(1)) if age_match else None
                # Address
                addr_el = card.select_one(".address, .ThatsThem-address")
                if addr_el:
                    person["addresses"] = [addr_el.get_text(strip=True)]
                # Phone
                phone_el = card.select_one(".phone, .ThatsThem-phone")
                if phone_el:
                    person["phones"] = [phone_el.get_text(strip=True)]
                # Email
                email_el = card.select_one(".email, .ThatsThem-email")
                if email_el:
                    person["emails"] = [email_el.get_text(strip=True)]

                if person.get("full_name"):
                    results.append(person)

            return {"source": "thatsthem", "results": results[:15]}
        except ImportError:
            return {"source": "thatsthem", "results": [], "error": "beautifulsoup4 not installed"}
        except Exception as e:
            return {"source": "thatsthem", "results": [], "error": str(e)}

    async def search_fastpeoplesearch(self, name: str, location: str | None = None) -> dict[str, Any]:
        """Search FastPeopleSearch."""
        try:
            slug = name.lower().replace(" ", "-")
            url = f"https://www.fastpeoplesearch.com/name/{quote_plus(slug)}"
            if location:
                url += f"_{quote_plus(location.lower().replace(' ', '-'))}"

            resp = await self._client.get(url)
            if resp.status_code != 200:
                return {"source": "fastpeoplesearch", "results": [], "error": f"HTTP {resp.status_code}"}

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            results = []

            for card in soup.select(".card.card-block, .people-list-item"):
                person: dict[str, Any] = {"source": "fastpeoplesearch"}
                name_el = card.select_one(".card-title, h2 a, .name")
                if name_el:
                    person["full_name"] = name_el.get_text(strip=True)
                detail_el = card.select_one(".card-text, .detail-text")
                if detail_el:
                    text = detail_el.get_text(strip=True)
                    # Try to extract age
                    age_match = re.search(r"Age\s*(\d+)", text)
                    person["age"] = int(age_match.group(1)) if age_match else None
                    # Try to extract location
                    person["addresses"] = [text] if text else []

                if person.get("full_name"):
                    results.append(person)

            return {"source": "fastpeoplesearch", "results": results[:15]}
        except ImportError:
            return {"source": "fastpeoplesearch", "results": [], "error": "beautifulsoup4 not installed"}
        except Exception as e:
            return {"source": "fastpeoplesearch", "results": [], "error": str(e)}

    async def search_all(self, name: str, location: str | None = None) -> list[dict[str, Any]]:
        """Run all people search engines concurrently."""
        tasks = [
            self.search_thatsthem(name, location),
            self.search_fastpeoplesearch(name, location),
        ]
        all_results: list[dict] = []
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, dict):
                all_results.extend(result.get("results", []))
        return all_results

    async def close(self) -> None:
        await self._client.close()


async def run_people_search(name: str, **kwargs: Any) -> dict[str, Any]:
    """Top-level entry point for people search."""
    connector = PeopleSearchConnector()
    try:
        location = kwargs.get("location")
        results = await connector.search_all(name, location)
        return {
            "name": name,
            "location": location,
            "results": results,
            "count": len(results),
        }
    finally:
        await connector.close()
