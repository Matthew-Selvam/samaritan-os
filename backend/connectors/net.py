"""
net.py — Hardened outbound HTTP + DNS core for every Signal-OS connector
=====================================================================
Every connector in this package goes through :func:`fetch` instead of calling
``httpx`` directly. It centralises the things that make a fetch *safe*:

* **Scheme allowlist** — only ``http``/``https``; ``file``, ``gopher``, ``ftp``,
  ``data``, ``javascript``, ``ldap`` are refused outright.
* **SSRF guard** — the host is resolved and *every* A/AAAA record is checked
  against loopback / private / link-local / CGNAT / reserved / multicast /
  cloud-metadata ranges. Redirects are followed manually and re-validated, so a
  public URL cannot bounce us onto ``169.254.169.254``.
* **Size caps** — response bodies are streamed and truncated at
  ``max_bytes`` (a decompression bomb cannot exhaust memory).
* **WS-SEC integration is optional** — if ``backend/security.py`` exists (it is
  owned by the WS-SEC workstream) we defer to ``security.safe_fetch`` and
  ``security.assert_public_host``. If that module is missing we fall back to the
  equivalent local guard here, so SSRF protection works *today* and gets
  stronger later without touching a single connector.
* **OPSEC** — when ``OPSEC_ENABLED`` is on, traffic rides the Tor SOCKS proxy
  from ``opsec_config`` (``.onion`` targets are only ever attempted that way).
  We deliberately do *not* silently fall back to a direct connection: that would
  leak the operator's exit IP.

Nothing here raises: every function returns a structured result carrying an
explicit ``error`` key.

Note on third-party imports: only ``httpx`` (already in requirements.txt) is
imported at module scope. ``security`` and ``opsec_config`` are imported lazily
inside functions so a missing module degrades instead of crashing.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

# ── Constants ────────────────────────────────────────────────────────────────

BLOCKED_SCHEMES = {"file", "gopher", "ftp", "data", "javascript", "ldap",
                   "mailto", "tel", "ws", "wss"}
ALLOWED_SCHEMES = {"http", "https"}

#: Default cap for a text/HTML response.
MAX_FETCH_BYTES = 8 * 1024 * 1024
#: Cap for a binary download (images, archives).
MAX_BINARY_BYTES = 25 * 1024 * 1024
#: Cap on any single local file a connector will read.
MAX_LOCAL_FILE_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 5

DEFAULT_UA = "Signal-OS/1.0 (+https://github.com/Matthew-Selvam/samaritan-os)"
JSON_HEADERS = {"accept": "application/dns-json, application/json", "user-agent": DEFAULT_UA}

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_METADATA_HOSTS = {"metadata.google.internal", "metadata.goog", "instance-data",
                   "metadata", "169.254.169.254"}
# Alibaba Cloud metadata endpoint — inside link-local but listed explicitly so
# the reason is legible in logs.
_METADATA_IPS = {"100.100.100.200"}


class NetError(RuntimeError):
    """Raised internally by the guard rails; never escapes this module."""


# ── IP / host classification ──────────────────────────────────────────────────

def ip_is_public(ip: str) -> bool:
    """True when *ip* is a routable public address.

    Rejects loopback, private, link-local (incl. 169.254.169.254), CGNAT,
    reserved, multicast, unspecified and the known cloud-metadata endpoints.

    Args:
        ip: an IPv4 or IPv6 literal.

    Returns:
        ``True`` only for addresses safe to connect to from the server.
    """
    try:
        addr = ipaddress.ip_address(str(ip).strip().strip("[]"))
    except ValueError:
        return False
    if str(addr) in _METADATA_IPS:
        return False
    if (addr.is_loopback or addr.is_private or addr.is_link_local or
            addr.is_reserved or addr.is_multicast or addr.is_unspecified):
        return False
    if addr.version == 4 and addr in _CGNAT:
        return False
    # IPv6 unique-local fd00::/8 is covered by is_private, but be explicit.
    if addr.version == 6 and addr in ipaddress.ip_network("fc00::/7"):
        return False
    return True


def is_onion(host: str) -> bool:
    """True when *host* is a Tor hidden-service (.onion) name."""
    return str(host or "").lower().rstrip(".").endswith(".onion")


async def _resolve_all(host: str) -> list[str]:
    """Resolve every A/AAAA record for *host* (blocking DNS in a thread).

    Args:
        host: hostname to resolve.

    Returns:
        List of IP literal strings; empty when resolution fails.
    """
    loop = asyncio.get_running_loop()

    def _lookup() -> list[str]:
        out: list[str] = []
        for fam in (socket.AF_INET, socket.AF_INET6):
            try:
                infos = socket.getaddrinfo(host, None, fam, socket.SOCK_STREAM)
            except (socket.gaierror, OSError, UnicodeError):
                continue
            for info in infos:
                ip = info[4][0]
                if ip not in out:
                    out.append(ip)
        return out

    try:
        return await loop.run_in_executor(None, _lookup)
    except Exception:  # noqa: BLE001 — resolution must never explode
        return []


async def guard_host(host: str, *, allow_private: bool | None = None) -> None:
    """Validate *host* is safe to connect to. Raises :class:`NetError` otherwise.

    Prefers the WS-SEC ``security.assert_public_host`` when that module is
    present (imported lazily); otherwise applies the identical local checks.

    Args:
        host: hostname or IP literal.
        allow_private: force-allow private ranges (used by tests and by
            connectors that explicitly target a self-hosted service).

    Raises:
        NetError: on a blocked scheme-equivalent host, a private/loopback/
            metadata address, or a name that does not resolve.
    """
    if not host:
        raise NetError("no host in url")
    host = str(host).strip().lower().strip("[]")
    if host in _METADATA_HOSTS:
        raise NetError(f"blocked cloud metadata host: {host}")
    if is_onion(host):
        # Hidden services have no public DNS; Tor routing is the control.
        return
    if allow_private is None:
        allow_private = os.getenv("ALLOW_PRIVATE_NETWORK", "false").lower() == "true"
    if allow_private:
        return

    # ── WS-SEC path (lazy: module may not exist yet) ────────────────────────
    try:
        from security import assert_public_host  # type: ignore  # lazy, optional
        await assert_public_host(host)
        return
    except ImportError:
        pass
    except NetError:
        raise
    except Exception as exc:  # SSRFError and friends
        raise NetError(str(exc)) from exc

    # ── Local fallback guard ────────────────────────────────────────────────
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        ips = await _resolve_all(host)
        if not ips:
            raise NetError(f"host does not resolve: {host}")
    for ip in ips:
        if not ip_is_public(ip):
            raise NetError(f"blocked non-public address {ip} for host {host}")


# ── Client construction ──────────────────────────────────────────────────────

def _opsec_proxy() -> str | None:
    """Tor proxy URL when OPSEC is enabled, else ``None``."""
    try:
        from opsec_config import OPSEC_ENABLED, TOR_PROXY  # lazy import
    except Exception:  # noqa: BLE001 — degrade to a direct connection
        return None
    return TOR_PROXY if OPSEC_ENABLED else None


def _build_client(timeout: float, proxy: str | None) -> httpx.AsyncClient:
    """Build an httpx client, optionally through the Tor SOCKS proxy."""
    transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None
    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        # Redirects are handled manually so every hop is re-guarded.
        follow_redirects=False,
        verify=True,
    )


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class FetchResult:
    """Uniform result of a guarded fetch.

    Attributes:
        ok: ``True`` when the request completed and a body was read.
        status_code: HTTP status, or ``0`` when no response was obtained.
        headers: response headers (lowercased keys), best-effort.
        content: response bytes, truncated at ``max_bytes``.
        text: decoded response text.
        truncated: ``True`` when the body hit the size cap.
        final_url: the URL the body actually came from (after redirects).
        error: explicit failure reason, ``None`` on success.
    """

    ok: bool = False
    status_code: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    text: str = ""
    truncated: bool = False
    final_url: str = ""
    error: str | None = None

    def json(self) -> Any:
        """Parse the body as JSON. Returns ``None`` when it is not valid JSON."""
        try:
            import json as _json
            return _json.loads(self.text or "null")
        except Exception:  # noqa: BLE001
            return None

    def __bool__(self) -> bool:
        return self.ok


def _err(msg: str, *, status: int = 0, url: str = "") -> FetchResult:
    return FetchResult(ok=False, status_code=status, final_url=url, error=msg)


async def _call_ws_sec_fetch(method: str, url: str, call_kwargs: dict[str, Any]) -> FetchResult | None:
    """Try the WS-SEC ``security.safe_fetch`` helper.

    Args:
        method: HTTP verb.
        url: absolute URL (already scheme-checked).
        call_kwargs: candidate keyword arguments.

    Returns:
        A :class:`FetchResult`, or ``None`` when ``security.safe_fetch`` is
        absent / has an incompatible signature so the caller should use the
        local implementation instead.
    """
    try:
        from security import safe_fetch  # type: ignore  # lazy, optional
    except Exception:  # noqa: BLE001 — WS-SEC not merged yet
        return None
    if not callable(safe_fetch):
        return None

    import inspect
    try:
        params = inspect.signature(safe_fetch).parameters
    except (TypeError, ValueError):
        params = {}
    has_var_kw = any(p.kind is p.VAR_KEYWORD for p in params.values())
    accepted = set(params)

    shapes = []
    filtered = dict(call_kwargs) if has_var_kw else {k: v for k, v in call_kwargs.items() if k in accepted}
    shapes.append({"url": url, "method": method, **filtered})
    shapes.append({**filtered, "url": url, "method": method})

    for shape in shapes:
        try:
            raw = safe_fetch(**shape)   # lazy optional WS-SEC module
            if hasattr(raw, "__await__"):
                raw = await raw
        except TypeError:
            continue
        except Exception as exc:  # noqa: BLE001 — WS-SEC blocked it; surface it
            return _err(str(exc), url=url)
        return _coerce(raw, url)
    return None


def _coerce(raw: Any, url: str) -> FetchResult:
    """Normalise whatever ``safe_fetch`` returned into a FetchResult."""
    if isinstance(raw, FetchResult):
        return raw
    if isinstance(raw, httpx.Response):
        try:
            body = raw.content[:MAX_FETCH_BYTES]
            truncated = len(raw.content) > MAX_FETCH_BYTES
        except Exception:  # noqa: BLE001
            body, truncated = b"", False
        return FetchResult(ok=raw.is_success, status_code=raw.status_code,
                           headers={k.lower(): v for k, v in raw.headers.items()},
                           content=body, text=body.decode("utf-8", "replace"),
                           truncated=truncated, final_url=str(raw.url),
                           error=None if raw.is_success else f"HTTP {raw.status_code}")
    if isinstance(raw, (dict, str, bytes)):
        if isinstance(raw, bytes):
            return FetchResult(ok=True, content=raw, text=raw.decode("utf-8", "replace"),
                               final_url=url)
        if isinstance(raw, str):
            return FetchResult(ok=True, status_code=200, headers={},
                               content=raw.encode("utf-8"), text=raw, final_url=url)
        if any(k in raw for k in ("status_code", "text", "body")):
            text = raw.get("text") or raw.get("body") or ""
            status = int(raw.get("status_code") or 0)
            return FetchResult(
                ok=bool(raw.get("ok", status < 400)),
                status_code=status,
                headers=raw.get("headers") or {},
                content=text.encode() if isinstance(text, str) else bytes(text or b""),
                text=text if isinstance(text, str) else "",
                final_url=raw.get("final_url") or url,
                error=raw.get("error"),
            )
    return _err(f"unrecognised safe_fetch return: {type(raw).__name__}", url=url)


async def fetch(
    method: str,
    url: str,
    *,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    data: Any = None,
    files: Any = None,
    content: bytes | None = None,
    max_bytes: int | None = None,
    allow_private: bool | None = None,
    allow_onion: bool = True,
    use_opsec: bool = True,
    _redirects: int = 0,
) -> FetchResult:
    """Perform one **guarded** outbound HTTP request.

    Args:
        method: HTTP verb (case-insensitive).
        url: absolute URL. Only ``http``/``https`` (and ``.onion`` when
            ``allow_onion``) are accepted.
        timeout: per-request timeout in seconds.
        headers: extra request headers.
        params: query-string parameters.
        json_body: JSON request body (mutually exclusive with ``data``).
        data: form body.
        files: multipart files.
        content: raw request body.
        max_bytes: response size cap (defaults to :data:`MAX_FETCH_BYTES`).
        allow_private: override the private-network block (self-hosted targets).
        allow_onion: permit ``.onion`` hosts.
        use_opsec: route through Tor when OPSEC is enabled.

    Returns:
        A :class:`FetchResult`. Never raises — failures carry ``error``.
    """
    try:
        return await _fetch_impl(
            method, url, timeout=timeout, headers=headers, params=params,
            json_body=json_body, data=data, files=files, content=content,
            max_bytes=max_bytes, allow_private=allow_private,
            allow_onion=allow_onion, use_opsec=use_opsec, _redirects=_redirects,
        )
    except NetError as exc:
        return _err(str(exc), url=url)
    except Exception as exc:  # noqa: BLE001 — a connector must never crash a pipeline
        return _err(f"{type(exc).__name__}: {exc}", url=url)


async def _fetch_impl(
    method: str, url: str, *, timeout: float, headers: dict | None,
    params: dict | None, json_body: Any, data: Any, files: Any,
    content: bytes | None, max_bytes: int | None, allow_private: bool | None,
    allow_onion: bool, use_opsec: bool, _redirects: int,
) -> FetchResult:
    """Internal body of :func:`fetch` (may raise :class:`NetError`)."""
    method = (method or "GET").upper()
    cap = int(max_bytes or MAX_FETCH_BYTES)
    parsed = urlparse(url or "")
    scheme = (parsed.scheme or "").lower()
    if scheme in BLOCKED_SCHEMES:
        raise NetError(f"blocked scheme: {scheme or '(none)'}")
    if scheme not in ALLOWED_SCHEMES:
        raise NetError(f"unsupported scheme: {scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise NetError(f"no host in url: {url[:80]}")
    if is_onion(host) and not allow_onion:
        raise NetError("onion targets disabled for this connector")

    await guard_host(host, allow_private=allow_private)

    proxy = _opsec_proxy() if use_opsec else None
    req_headers = {"user-agent": DEFAULT_UA, "accept-encoding": "gzip, deflate"}
    req_headers.update(headers or {})

    # ── Try the WS-SEC helper first when it exists ──────────────────────────
    if _redirects == 0:
        wssec = await _call_ws_sec_fetch(method, url, {
            "timeout": timeout, "headers": req_headers, "params": params,
            "json": json_body, "data": data, "files": files,
            "content": content, "max_bytes": cap,
            # allow_private MUST be forwarded: without it safe_fetch falls back
            # to the env default and refuses a connector's explicitly local
            # target (Ollama on :11434, Qdrant on :6333).
            "allow_private": allow_private,
        })
        if wssec is not None:
            if wssec.ok and 300 <= wssec.status_code < 400 and wssec.headers.get("location"):
                return await _follow(wssec.headers["location"], url, method, locals())
            return wssec

    client = _build_client(timeout, proxy)
    try:
        async with client.stream(
            method, url, headers=req_headers, params=params,
            json=json_body, data=data, files=files, content=content,
        ) as resp:
            if 300 <= resp.status_code < 400:
                location = resp.headers.get("location")
                if location and _redirects < MAX_REDIRECTS:
                    return await _follow(location, url, method, locals())
                return _err(f"HTTP {resp.status_code} redirect without location", status=resp.status_code, url=url)

            chunks: list[bytes] = []
            total = 0
            truncated = False
            async for chunk in resp.aiter_bytes():
                room = cap - total
                if room <= 0:
                    truncated = True
                    break
                if len(chunk) > room:
                    chunks.append(chunk[:room])
                    total = cap
                    truncated = True
                    break
                chunks.append(chunk)
                total += len(chunk)
            body = b"".join(chunks)
            text = body.decode(resp.encoding or "utf-8", "replace")
            return FetchResult(
                ok=200 <= resp.status_code < 300,
                status_code=resp.status_code,
                headers={k.lower(): v for k, v in resp.headers.items()},
                content=body, text=text, truncated=truncated,
                final_url=str(resp.url),
                error=None if 200 <= resp.status_code < 300 else f"HTTP {resp.status_code}",
            )
    finally:
        await client.aclose()


async def _follow(location: str, base_url: str, method: str, prev: dict) -> FetchResult:
    """Re-validate and follow one redirect hop (SSRF-safe)."""
    from urllib.parse import urljoin
    nxt = urljoin(base_url, location)
    if _redirects_count(prev) >= MAX_REDIRECTS:
        return _err("too many redirects", url=base_url)
    return await _fetch_impl(
        "GET" if method in ("POST", "PUT", "PATCH") else method, nxt,
        timeout=prev.get("timeout", 20.0), headers=prev.get("headers"),
        params=None, json_body=None, data=None, files=None, content=None,
        max_bytes=prev.get("max_bytes"), allow_private=prev.get("allow_private"),
        allow_onion=prev.get("allow_onion", True), use_opsec=prev.get("use_opsec", True),
        _redirects=_redirects_count(prev) + 1,
    )


def _redirects_count(scope: dict) -> int:
    return int(scope.get("_redirects", 0) or 0)


# ── Convenience wrappers ─────────────────────────────────────────────────────

async def fetch_text(url: str, **kw: Any) -> str | None:
    """GET *url* through the guarded path and return its text (``None`` on failure)."""
    res = await fetch("GET", url, **kw)
    return res.text if res.ok else None


async def fetch_json(url: str, **kw: Any) -> Any:
    """GET *url* and parse JSON. Returns ``None`` on any failure."""
    res = await fetch("GET", url, headers={"accept": "application/json", **(kw.pop("headers", None) or {})}, **kw)
    return res.json() if res.ok else None


async def post_json(url: str, payload: Any = None, **kw: Any) -> Any:
    """POST a JSON body through the guarded path and parse the JSON reply."""
    res = await fetch("POST", url, json_body=payload,
                      headers={"content-type": "application/json",
                               "accept": "application/json",
                               **(kw.pop("headers", None) or {})}, **kw)
    return res.json() if res.ok else None


# ── DNS (used by email MX/SPF/DMARC + subdomain enumeration) ─────────────────

_DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)
_TTL = 300.0
_dns_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


async def resolve_dns(name: str, rtype: str = "A", *, timeout: float = 8.0) -> list[str]:
    """Resolve DNS records without dnspython (DNS-over-HTTPS + system resolver).

    Args:
        name: record name (e.g. ``example.com``).
        rtype: record type as a string (``A``, ``AAAA``, ``MX``, ``TXT``,
            ``NS``, ``CNAME``, ``SOA``).
        timeout: per-endpoint timeout.

    Returns:
        Normalised record values; empty list when nothing resolved. Never raises.
    """
    name = (name or "").strip().rstrip(".")
    rtype = (rtype or "A").upper()
    if not name:
        return []
    key = (name.lower(), rtype)
    hit = _dns_cache.get(key)
    if hit and hit[0] > time.time():
        return list(hit[1])

    records: list[str] = []
    for endpoint in _DOH_ENDPOINTS:
        res = await fetch(endpoint, params={"name": name, "type": rtype},
                          headers={"accept": "application/dns-json"}, timeout=timeout)
        if not res.ok:
            continue
        payload = res.json()
        if not isinstance(payload, dict):
            continue
        if int(payload.get("Status", 0) or 0) not in (0, 3):
            continue
        for answer in payload.get("Answer") or []:
            data = str(answer.get("data", "")).strip()
            if not data:
                continue
            data = data.strip('"')
            if rtype in ("MX", "NS"):
                parts = data.split()
                data = parts[-1].rstrip(".") if parts else data
            records.append(data)
        break

    if not records and rtype in ("A", "AAAA"):
        # Last resort: the system resolver (DoH endpoints may be blocked).
        try:
            ips = await _resolve_all(name)
            records = [ip for ip in ips
                       if (":" in ip) == (rtype == "AAAA")]
        except Exception:  # noqa: BLE001
            records = []

    _dns_cache[key] = (time.time() + _TTL, records)
    return records


def dns_cache_clear() -> int:
    """Empty the in-process DNS cache. Returns the number of entries dropped."""
    n = len(_dns_cache)
    _dns_cache.clear()
    return n


# ── Local-file guards (decompression bombs, absurd sizes) ────────────────────

def guard_local_file(path: str, *, max_bytes: int = MAX_LOCAL_FILE_BYTES,
                     extensions: tuple[str, ...] | None = None) -> str | None:
    """Validate a local path before a connector opens it.

    Args:
        path: filesystem path.
        max_bytes: hard size ceiling.
        extensions: when given, the allowed lowercase extensions.

    Returns:
        ``None`` when the file is safe to read, else an error message string.
    """
    if not path:
        return "empty path"
    if extensions:
        ext = os.path.splitext(str(path))[1].lower()
        if ext not in tuple(e.lower() for e in extensions):
            return f"unsupported file type '{ext or '(none)'}' — expected one of {', '.join(extensions)}"
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return f"cannot stat file: {exc.strerror or exc}"
    if size == 0:
        return "file is empty"
    if size > max_bytes:
        return f"file too large: {size} bytes (cap {max_bytes})"
    return None


__all__ = [
    "BLOCKED_SCHEMES", "ALLOWED_SCHEMES", "MAX_FETCH_BYTES", "MAX_BINARY_BYTES",
    "MAX_LOCAL_FILE_BYTES", "NetError", "FetchResult", "fetch", "fetch_text",
    "fetch_json", "post_json", "guard_host", "guard_local_file", "ip_is_public",
    "is_onion", "resolve_dns", "dns_cache_clear", "DEFAULT_UA",
]