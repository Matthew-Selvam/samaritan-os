"""
opsec.py — Anonymized HTTP Client
===================================
Drop-in replacement for httpx.AsyncClient that routes ALL traffic through
Tor/SOCKS5, randomizes fingerprints, and adds request jitter.

Usage:
    async with OpsecClient() as client:
        resp = await client.get("https://example.com")

Nothing touches the internet naked.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from opsec_config import (
    TOR_PROXY, TOR_CONTROL, TOR_PASSWORD, OPSEC_ENABLED,
    random_headers, jitter,
)

log = logging.getLogger("opsec")


class OpsecClient:
    """Anonymized async HTTP client.  Wraps httpx with Tor routing,
    header randomization, and request jitter."""

    def __init__(self, proxy: str | None = None, timeout: float = 30.0):
        self._proxy = proxy or (TOR_PROXY if OPSEC_ENABLED else None)
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._request_count = 0

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            transport = None
            if self._proxy:
                transport = httpx.AsyncHTTPTransport(proxy=self._proxy)
            self._client = httpx.AsyncClient(
                transport=transport,
                timeout=self._timeout,
                follow_redirects=True,
                verify=True,
            )
        return self._client

    async def _prepare(self, extra_headers: dict | None = None) -> dict[str, str]:
        """Apply jitter + randomized headers before each request."""
        delay = jitter()
        await asyncio.sleep(delay)
        headers = random_headers()
        if extra_headers:
            headers.update(extra_headers)
        self._request_count += 1
        return headers

    # ── HTTP verbs ────────────────────────────────────────────────────────────

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        client = await self._ensure_client()
        kwargs["headers"] = await self._prepare(kwargs.pop("headers", None))
        return await client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        client = await self._ensure_client()
        kwargs["headers"] = await self._prepare(kwargs.pop("headers", None))
        return await client.post(url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> httpx.Response:
        client = await self._ensure_client()
        kwargs["headers"] = await self._prepare(kwargs.pop("headers", None))
        return await client.head(url, **kwargs)

    # ── Tor management ────────────────────────────────────────────────────────

    async def test_tor(self) -> dict[str, Any]:
        """Verify Tor connectivity by hitting the Tor Project check API."""
        try:
            client = await self._ensure_client()
            headers = random_headers()
            resp = await client.get(
                "https://check.torproject.org/api/ip",
                headers=headers,
            )
            data = resp.json()
            return {
                "connected": data.get("IsTor", False),
                "ip": data.get("IP", "unknown"),
            }
        except Exception as e:
            log.warning("Tor check failed: %s", e)
            return {"connected": False, "ip": "unknown", "error": str(e)}

    async def new_circuit(self) -> dict[str, Any]:
        """Request a new Tor circuit (new exit node / IP).
        Sends SIGNAL NEWNYM to the Tor control port."""
        try:
            host, port_str = TOR_CONTROL.split(":")
            port = int(port_str)
            reader, writer = await asyncio.open_connection(host, port)

            # Authenticate
            if TOR_PASSWORD:
                writer.write(f'AUTHENTICATE "{TOR_PASSWORD}"\r\n'.encode())
            else:
                writer.write(b'AUTHENTICATE\r\n')
            await writer.drain()
            auth_resp = await reader.readline()

            if b"250" not in auth_resp:
                writer.close()
                return {"success": False, "error": f"Auth failed: {auth_resp.decode().strip()}"}

            # Request new circuit
            writer.write(b"SIGNAL NEWNYM\r\n")
            await writer.drain()
            signal_resp = await reader.readline()
            writer.close()

            success = b"250" in signal_resp
            if success:
                log.info("New Tor circuit requested")
            return {"success": success, "response": signal_resp.decode().strip()}
        except Exception as e:
            log.warning("New circuit request failed: %s", e)
            return {"success": False, "error": str(e)}

    async def status(self) -> dict[str, Any]:
        """Full OPSEC status report."""
        tor_info = await self.test_tor() if OPSEC_ENABLED else {"connected": False, "ip": "direct"}
        return {
            "opsec_enabled": OPSEC_ENABLED,
            "proxy": self._proxy or "direct",
            "tor_connected": tor_info.get("connected", False),
            "current_ip": tor_info.get("ip", "unknown"),
            "requests_routed": self._request_count,
        }

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> OpsecClient:
        await self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


# Module-level singleton for quick use
_default_client: OpsecClient | None = None


async def get_opsec_client() -> OpsecClient:
    """Get or create the module-level OpsecClient singleton."""
    global _default_client
    if _default_client is None:
        _default_client = OpsecClient()
    return _default_client
