"""
breach_check.py — Breach Correlation Connector
================================================
Have I Been Pwned + breach database lookups through the OPSEC layer.
"""
from __future__ import annotations

import os
from typing import Any

from opsec import OpsecClient

HIBP_API_KEY = os.getenv("HIBP_API_KEY", "")


class BreachCheckConnector:
    """Check emails/usernames against breach databases."""

    def __init__(self) -> None:
        self._client = OpsecClient(timeout=15.0)

    async def check_hibp(self, account: str) -> dict[str, Any]:
        """Query Have I Been Pwned API v3.

        Requires HIBP_API_KEY. Returns list of breaches the account appears in.
        """
        if not HIBP_API_KEY:
            return {"target": account, "breaches": [],
                    "error": "HIBP_API_KEY not set — get one at haveibeenpwned.com/API/Key"}

        try:
            resp = await self._client.get(
                f"https://haveibeenpwned.com/api/v3/breachedaccount/{account}",
                headers={
                    "hibp-api-key": HIBP_API_KEY,
                    "User-Agent": "Samaritan-OS-OSINT",
                },
                params={"truncateResponse": "false"},
            )

            if resp.status_code == 404:
                return {"target": account, "breaches": [], "total_breaches": 0}
            if resp.status_code == 429:
                return {"target": account, "breaches": [],
                        "error": "Rate limited by HIBP — try again later"}
            resp.raise_for_status()

            breaches = resp.json()
            return {
                "target": account,
                "breaches": [
                    {
                        "name": b.get("Name"),
                        "title": b.get("Title"),
                        "domain": b.get("Domain"),
                        "date": b.get("BreachDate"),
                        "data_types": b.get("DataClasses", []),
                        "records": b.get("PwnCount", 0),
                        "description": b.get("Description", "")[:200],
                        "is_verified": b.get("IsVerified", False),
                    }
                    for b in breaches
                ],
                "total_breaches": len(breaches),
            }
        except Exception as e:
            return {"target": account, "breaches": [], "error": str(e)}

    async def check_pastes(self, email: str) -> dict[str, Any]:
        """Check if an email appears in public pastes (HIBP)."""
        if not HIBP_API_KEY:
            return {"target": email, "pastes": [], "error": "HIBP_API_KEY not set"}

        try:
            resp = await self._client.get(
                f"https://haveibeenpwned.com/api/v3/pasteaccount/{email}",
                headers={
                    "hibp-api-key": HIBP_API_KEY,
                    "User-Agent": "Samaritan-OS-OSINT",
                },
            )

            if resp.status_code == 404:
                return {"target": email, "pastes": [], "total_pastes": 0}
            resp.raise_for_status()

            pastes = resp.json()
            return {
                "target": email,
                "pastes": [
                    {
                        "source": p.get("Source"),
                        "title": p.get("Title"),
                        "date": p.get("Date"),
                        "email_count": p.get("EmailCount", 0),
                    }
                    for p in pastes
                ],
                "total_pastes": len(pastes),
            }
        except Exception as e:
            return {"target": email, "pastes": [], "error": str(e)}

    async def close(self) -> None:
        await self._client.close()


async def run_breach_check(target: str, **kwargs: Any) -> dict[str, Any]:
    """Top-level entry point for breach checking."""
    connector = BreachCheckConnector()
    try:
        breaches = await connector.check_hibp(target)
        pastes = await connector.check_pastes(target) if "@" in target else {"pastes": []}

        return {
            "target": target,
            "breaches": breaches.get("breaches", []),
            "total_breaches": breaches.get("total_breaches", 0),
            "pastes": pastes.get("pastes", []),
            "total_pastes": pastes.get("total_pastes", 0),
            "error": breaches.get("error") or pastes.get("error"),
        }
    finally:
        await connector.close()
