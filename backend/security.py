"""
security.py — Signal-OS outbound-fetch + filesystem hardening (WS-SEC)
======================================================================
Signal-OS is an OSINT platform: it fetches attacker-supplied URLs from the
*server* (CRAWLER, reverse-image, breach connectors, spiderfoot, searxng).
That makes SSRF the single most dangerous bug class in this codebase, because
the fetch runs inside the deployment's own network namespace:

* ``http://169.254.169.254/latest/meta-data/iam/security-credentials/`` — AWS /
  GCP / Azure / Alibaba / DigitalOcean instance credentials ⇒ full cloud
  account takeover from an anonymous HTTP request.
* ``http://localhost:6379/`` — the Redis cache/queue holding investigation
  results; ``http://10.0.0.5:5432/`` — the Postgres cases store.
* ``http://[::1]:8080/`` — the OPSEC/Tor control plane.
* ``file:///etc/passwd`` — local filesystem reads.

Everything in this module exists to make that impossible:

:func:`assert_public_host`
    Resolves the hostname through the running event loop's ``getaddrinfo`` and
    rejects **every** A/AAAA record that lands in loopback, RFC1918,
    link-local (incl. the metadata endpoint), CGNAT, IETF-reserved, multicast
    or IPv6-ULA space. DNS resolution — not string matching — is what makes it
    hold against ``http://localtest.me/`` or a rebinding record.
:func:`safe_fetch`
    The hardened fetch helper every outbound path must use: scheme + host
    validation, **manual** redirect handling that re-validates each hop,
    streaming byte cap (a decompression bomb cannot exhaust memory),
    content-type allowlist and an honest User-Agent. Network/SSRF failures come
    back as ``{"error": ...}`` instead of raising, because a crawler must never
    take down a pipeline.

``ALLOW_PRIVATE_NETWORK=true`` disables the private-range checks **and** the
DNS resolution they depend on. That is required for the docker-compose stack,
where ``searxng`` and ``spiderfoot`` are private service names — see
:func:`private_network_allowed` for the full trade-off. Never enable it on a
host where the process can reach cloud metadata.

This module imports only the standard library plus ``httpx`` (already in
requirements) at module scope; ``observability`` is imported defensively so a
missing logger can never break import.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse

try:  # pragma: no cover - logging must never break import
    from observability import get_logger
except Exception:  # noqa: BLE001 - degrade to stdlib logging
    import logging

    def get_logger(name: str) -> logging.Logger:  # type: ignore[misc]
        """Minimal stdlib fallback for the WS-INFRA logger."""
        return logging.getLogger(name)


log = get_logger("signal-os.security")

__all__ = [
    "SSRFError",
    "PathTraversalError",
    "BLOCKED_SCHEMES",
    "ALLOWED_SCHEMES",
    "BLOCKED_HOSTS",
    "ALLOWED_CONTENT_TYPES",
    "MAX_DOWNLOAD_BYTES",
    "MAX_UPLOAD_BYTES",
    "MAX_REDIRECTS",
    "MAX_URL_LENGTH",
    "DEFAULT_UA",
    "is_safe_url",
    "assert_public_host",
    "ip_is_blocked",
    "sanitize_filename",
    "safe_join",
    "private_network_allowed",
    "safe_fetch",
    "read_upload_limited",
    "rate_limit_headers",
    "client_ip",
]

# ── Constants ────────────────────────────────────────────────────────────────

#: Schemes that must never be handed to an HTTP client. The first six are the
#: contract set; the rest are equally fatal (they reach local handlers, the
#: browser engine, or a mail/tel dialer).
BLOCKED_SCHEMES = frozenset({
    "file", "gopher", "ftp", "data", "javascript", "ldap",
    "mailto", "tel", "ws", "wss", "blob", "about", "jar", "netdoc",
    "php", "expect", "dict", "smb",
})

#: The only schemes an outbound OSINT fetch may use.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Hostnames that exist purely to reach cloud credentials or cluster-internal
#: control planes. Blocked by name as well as by resolved address, because a
#: few of them (GCP) resolve to a link-local address that could change.
BLOCKED_HOSTS = frozenset({
    "metadata.google.internal",     # GCP
    "metadata.goog",               # GCP alias
    "metadata",                    # GCP short form
    "instance-data",               # GCP short form
    "instance-data.ec2.internal",  # AWS IMDS alias
    "169.254.169.254",             # AWS/Azure/DO IMDS
    "169.254.170.2",               # AWS ECS task metadata
    "100.100.100.200",             # Alibaba Cloud metadata
    "fd00:ec2::254",               # AWS IMDS over IPv6
    "kubernetes.default.svc",      # in-cluster API server
})

#: Hard cap on a downloaded response body (decompressed).
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
#: Hard cap on an uploaded body.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
#: Maximum redirect hops followed by :func:`safe_fetch`.
MAX_REDIRECTS = 5
#: Longest URL accepted (guards against parser/abuse blowups).
MAX_URL_LENGTH = 4096
#: Per-hop DNS/connect timeout for the pre-flight host check.
DNS_TIMEOUT_S = 5.0

DEFAULT_UA = "Signal-OS/1.0 (+https://github.com/Matthew-Selvam/samaritan-os)"

#: Content types an outbound fetch may return. Binary-by-default is a
#: deliberate choice: an OSINT crawler reads HTML/JSON/text (and images for
#: deanonymization); ``application/octet-stream`` would turn the crawler into a
#: drive-by downloader. Callers that genuinely want a binary must pass
#: ``allowed_content_types=(...)`` explicitly.
ALLOWED_CONTENT_TYPES: tuple[str, ...] = (
    "text/",
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/x-javascript",
    "application/ecmascript",
    "application/x-ndjson",
    "application/rss+xml",
    "application/atom+xml",
    "application/pdf",
    "image/",
    "audio/",
    "video/",
)

#: Header names stripped when a redirect crosses to a different origin.
_SENSITIVE_HOP_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})

#: Ranges that must never be the target of a server-side fetch. Listed
#: explicitly (rather than relying on ``ipaddress.is_private``) so behaviour is
#: identical across Python versions and so the intent is auditable.
_BLOCKED_V4_NETWORKS: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.ip_network(n) for n in (
        "0.0.0.0/8",          # "this network" — includes 0.0.0.0
        "10.0.0.0/8",         # RFC1918
        "100.64.0.0/10",      # CGNAT (RFC6598)
        "127.0.0.0/8",        # loopback
        "169.254.0.0/16",     # link-local, incl. 169.254.169.254 IMDS
        "172.16.0.0/12",      # RFC1918
        "192.0.0.0/24",       # IETF protocol assignments
        "192.0.2.0/24",       # TEST-NET-1
        "192.168.0.0/16",     # RFC1918
        "198.18.0.0/15",      # benchmarking (RFC2544)
        "198.51.100.0/24",    # TEST-NET-2
        "203.0.113.0/24",     # TEST-NET-3
        "224.0.0.0/4",        # multicast
        "240.0.0.0/4",        # reserved (incl. 255.255.255.255 broadcast)
    )
)

_BLOCKED_V6_NETWORKS: tuple[ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(n) for n in (
        "::/128",             # unspecified
        "::1/128",            # loopback
        "100::/64",           # discard-only
        "2001:db8::/32",      # documentation
        "2001::/32",          # Teredo
        "2001:2::/48",        # benchmarking
        "fc00::/7",           # unique local addresses (ULA)
        "fe80::/10",          # link-local
        "fec0::/10",          # site-local (deprecated)
        "ff00::/8",           # multicast
    )
)

#: NAT64 well-known prefix: an attacker can smuggle a private IPv4 target
#: through it (``64:ff9b::7f00:1`` == 127.0.0.1).
_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")

#: 6to4 embeds an IPv4 address in the next 32 bits.
_6TO4_NETWORK = ipaddress.ip_network("2002::/16")


# ── Errors ───────────────────────────────────────────────────────────────────

class SSRFError(ValueError):
    """Raised when a URL or host would let the server reach a non-public target.

    A ``ValueError`` subclass so the existing connector contract (which already
    catches ``ValueError`` around URL parsing) treats it as a bad input rather
    than an internal failure.
    """


class PathTraversalError(ValueError):
    """Raised when a caller-supplied path escapes its intended root."""


# ── Configuration helpers ────────────────────────────────────────────────────

def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var.

    Args:
        name: Environment variable name.
        default: Value when unset.

    Returns:
        The parsed boolean.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def private_network_allowed() -> bool:
    """Whether private/loopback destinations are permitted.

    ``ALLOW_PRIVATE_NETWORK=true`` (or ``ENVIRONMENT=development``) turns the
    SSRF guard off so a docker-compose deployment can reach its own
    ``searxng`` and ``spiderfoot`` services by service name.

    **Trade-off:** with this on, ``http://169.254.169.254/`` becomes a valid
    target again and any API caller can reach every internal service the
    backend can see. It is acceptable for a local dev stack or an isolated
    single-tenant box, and unacceptable for anything internet-facing — put a
    reverse proxy with its own egress policy in front instead.

    Returns:
        ``True`` when the private-network checks are disabled.
    """
    if _env_flag("ALLOW_PRIVATE_NETWORK", False):
        return True
    return os.getenv("ENVIRONMENT", "").strip().lower() == "development"


# ── IP classification ────────────────────────────────────────────────────────

def ip_is_blocked(ip: Any, *, allow_private: bool = False) -> str | None:
    """Classify a single IP address.

    Args:
        ip: ``ipaddress`` address object or a string literal.
        allow_private: Skip the range checks entirely (docker-compose mode).

    Returns:
        ``None`` when the address is a safe public target, otherwise a short
        human-readable reason such as ``"loopback"`` or ``"cloud metadata"``.
    """
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip.strip().strip("[]"))
        except ValueError:
            return None
    if allow_private:
        return None
    if ip.version == 4:
        return _blocked_reason_v4(ip)  # type: ignore[arg-type]
    return _blocked_reason_v6(ip)  # type: ignore[arg-type]


def _blocked_reason_v4(ip: ipaddress.IPv4Address) -> str | None:
    """Reason an IPv4 address is unsafe, or ``None``."""
    if str(ip) == "169.254.169.254" or str(ip) == "100.100.100.200":
        return "cloud metadata endpoint"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified address"
    if str(ip) == "255.255.255.255":
        return "broadcast"
    for net in _BLOCKED_V4_NETWORKS:
        if ip in net:
            return f"reserved range {net.with_prefixlen}"
    return None


def _blocked_reason_v6(ip: ipaddress.IPv6Address) -> str | None:
    """Reason an IPv6 address is unsafe, or ``None``."""
    # IPv4-mapped (::ffff:127.0.0.1) and IPv4-compatible addresses must be
    # judged on the embedded IPv4 address, not on the v6 properties.
    mapped = ip.ipv4_mapped
    if mapped is None and ip.sixtofour:
        mapped = ip.sixtofour
    if mapped is not None:
        return _blocked_reason_v4(mapped) or "IPv4-mapped IPv6 address"
    if ip.teredo:
        return "Teredo tunnel"
    if ip in _NAT64_NETWORK:
        embedded = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
        return _blocked_reason_v4(embedded) or "NAT64-mapped IPv4 address"  # type: ignore[arg-type]
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_site_local:
        return "site-local"
    if ip in _6TO4_NETWORK:
        embedded = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
        return _blocked_reason_v4(embedded) or "6to4-embedded address"  # type: ignore[arg-type]
    for net in _BLOCKED_V6_NETWORKS:
        if ip in net:
            return f"reserved range {net.with_prefixlen}"
    return None


def _parse_ip_literal(host: str) -> ipaddress._BaseAddress | None:
    """Parse *host* as an IP literal, including legacy integer/hex forms.

    ``http://2130706433/`` and ``http://0x7f.0.0.1/`` both mean 127.0.0.1 to a
    real HTTP client; ``ipaddress.ip_address`` alone would not see it.

    Args:
        host: Host component of a URL, already lowercased and unbracketed.

    Returns:
        The parsed address, or ``None`` when *host* is a real hostname.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # inet_aton understands "2130706433", "0177.0.0.1", "127.1".
    try:
        packed = socket.inet_aton(host)
    except (OSError, UnicodeError):
        return None
    return ipaddress.ip_address(packed)


def _normalize_host(host: str) -> str:
    """Lowercase, unbracket and de-zone an IPv6 host.

    Args:
        host: Raw host string.

    Returns:
        The normalized host.
    """
    host = str(host or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if "%" in host:  # IPv6 zone id: fe80::1%eth0
        host = host.split("%", 1)[0]
    return host.rstrip(".")  # trailing dot: "localhost." == "localhost"


# ── Host validation ──────────────────────────────────────────────────────────

async def assert_public_host(host: str, *, allow_private: bool | None = None) -> None:
    """Raise :class:`SSRFError` unless *host* resolves only to public addresses.

    Resolution goes through the running event loop's ``getaddrinfo`` (in a
    worker thread, so the loop is never blocked) and **every** A and AAAA record
    is inspected — a name with one public and one loopback record is rejected,
    because the client is free to pick the record that answers.

    Args:
        host: Hostname or IP literal.
        allow_private: Force-allow private ranges. ``None`` defers to
            :func:`private_network_allowed`.

    Raises:
        SSRFError: The host is empty, unresolvable, a known metadata name, or
            resolves to any non-public address.
    """
    host = _normalize_host(host)
    if not host:
        raise SSRFError("no host to validate")
    if allow_private is None:
        allow_private = private_network_allowed()
    if host in BLOCKED_HOSTS:
        raise SSRFError(f"blocked cloud metadata host: {host}")
    if allow_private:
        return

    literal = _parse_ip_literal(host)
    if literal is not None:
        reason = ip_is_blocked(literal, allow_private=False)
        if reason:
            raise SSRFError(f"blocked address {literal} ({reason}) for host {host}")
        return

    # `.onion` has no public DNS record: it is only routable through the Tor
    # proxy, and a direct connection to it fails. Accept the name, let the
    # transport decide.
    if host.endswith(".onion"):
        return

    ips = await _resolve_all(host)
    if not ips:
        raise SSRFError(f"host does not resolve: {host}")
    for ip in ips:
        reason = ip_is_blocked(ip, allow_private=False)
        if reason:
            raise SSRFError(
                f"host {host} resolves to blocked address {ip} ({reason})"
            )


async def _resolve_all(host: str) -> list[str]:
    """Resolve every A/AAAA record for *host*.

    Args:
        host: Hostname to resolve.

    Returns:
        IP literal strings; empty when resolution fails or times out.
    """
    loop = asyncio.get_running_loop()

    def _lookup() -> list[str]:
        out: list[str] = []
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                infos = socket.getaddrinfo(host, None, family, socket.SOCK_STREAM)
            except (socket.gaierror, OSError, UnicodeError):
                continue
            for info in infos:
                ip = str(info[4][0]).split("%", 1)[0]
                if ip not in out:
                    out.append(ip)
        return out

    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _lookup), DNS_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 - never explode
        log.debug("dns resolution failed for %s: %s", host, exc)
        return []


def is_safe_url(url: str, *, allow_private: bool | None = None) -> bool:
    """Cheap, synchronous URL validation (scheme + literal-address check).

    This is the fast path used before work is queued. It does **not** resolve
    DNS — only :func:`assert_public_host` can decide a *hostname* is safe, so
    every fetch must still call it.

    Args:
        url: Candidate URL.
        allow_private: Skip the private-range checks (``None`` reads the env).

    Returns:
        ``True`` when the URL parses, uses an allowed scheme, has a host, and
        does not point at a literal private/metadata address.
    """
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        return False
    if any(ch in url for ch in ("\n", "\r", "\t", " ", "\x00")):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    if not scheme or scheme in BLOCKED_SCHEMES or scheme not in ALLOWED_SCHEMES:
        return False
    try:
        raw_host = parsed.hostname
    except ValueError:
        return False
    if not raw_host:
        return False
    host = _normalize_host(raw_host)
    if not host or host in BLOCKED_HOSTS:
        return False
    # A subdomain of a blocked name is also a metadata target in some cloud
    # setups; a *super*domain of it (metadata.google.internal.evil.com) is a
    # legitimate public host and is decided by real DNS in
    # ``assert_public_host``.
    if any(host.endswith("." + blocked) for blocked in BLOCKED_HOSTS):
        return False
    if allow_private is None:
        allow_private = private_network_allowed()
    if allow_private:
        return True
    literal = _parse_ip_literal(host)
    if literal is not None:
        return ip_is_blocked(literal, allow_private=False) is None
    # A public hostname always has a dot. Single-label names are intranet
    # ("redis", "searxng", "localhost") and must not be resolved by a fetcher.
    if "." not in host or host.endswith(".local") or host.endswith(".internal"):
        return False
    return True


# ── Filesystem hardening ─────────────────────────────────────────────────────

_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff",
    ".pdf", ".txt", ".csv", ".json", ".xml", ".html", ".zip",
})
MAX_FILENAME_LENGTH = 128


def sanitize_filename(name: str, *, default: str = "upload.bin") -> str:
    """Reduce an attacker-supplied filename to a safe basename.

    The upload path in ``main.py`` used the client filename verbatim as a
    tempfile suffix, which allowed ``../../etc/cron.d/x`` style traversal and
    absurd suffixes.

    Args:
        name: Raw filename from the client (may be ``None``-ish/hostile).
        default: Returned when nothing usable survives sanitization.

    Returns:
        A basename with no separators, no ``..``, only ``[A-Za-z0-9._-]`` and a
        recognised extension, at most :data:`MAX_FILENAME_LENGTH` characters.
    """
    raw = str(name or "").strip()
    # Strip both POSIX and Windows directory components, plus any URL encoding
    # a client may have smuggled through a proxy.
    raw = raw.replace("\\", "/").replace("%2f", "/").replace("%2F", "/")
    raw = raw.rsplit("/", 1)[-1]
    raw = "".join(ch for ch in raw if ch.isprintable() and ch != "\x00")
    stem, dot, ext = raw.rpartition(".")
    if not dot:
        stem, ext = raw, ""
    ext = _UNSAFE_FILENAME_RE.sub("", ext)[:8].lower()
    stem = _UNSAFE_FILENAME_RE.sub("_", stem).strip("._")
    if not stem:
        # A name made entirely of separators/dots ("///", "...") has no usable
        # stem — fall back to the caller's default verbatim, so the returned
        # name is always the safe, known-good "upload.bin".
        return default
    if ext and f".{ext}" not in _SAFE_EXTENSIONS:
        ext = ""  # never trust an unknown extension into a content sniffer
    out = f"{stem[:MAX_FILENAME_LENGTH]}.{ext}" if ext else stem[:MAX_FILENAME_LENGTH]
    return out or default


def safe_join(root: str | os.PathLike[str], *parts: str) -> str:
    """Join *parts* under *root*, refusing any traversal.

    Args:
        root: Directory the result must stay inside.
        *parts: Untrusted path segments. Absolute paths, drive letters, NUL
            bytes and ``..`` segments are rejected.

    Returns:
        The absolute joined path.

    Raises:
        PathTraversalError: A segment is absolute, contains ``..``/NUL, or the
            result would escape *root*.
    """
    base = Path(root).resolve()
    segments: list[str] = []
    for part in parts:
        raw = str(part or "").replace("\\", "/")
        if not raw:
            continue
        if "\x00" in raw:
            raise PathTraversalError("NUL byte in path")
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            raise PathTraversalError(f"absolute path segment rejected: {raw[:60]}")
        for seg in raw.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                raise PathTraversalError("parent directory segment rejected")
            segments.append(seg)
    target = base.joinpath(*segments).resolve() if segments else base
    if target != base and base not in target.parents:
        raise PathTraversalError(f"resolved path escapes root: {target}")
    return str(target)


# ── Upload guard (closes the photo-search disk-exhaustion hole) ──────────────

async def read_upload_limited(
    source: Any,
    *,
    max_bytes: int | None = None,
    allowed_content_types: Sequence[str] | None = None,
    filename: str | None = None,
    chunk_size: int = 1024 * 1024,
) -> dict[str, Any]:
    """Read an upload to memory with a hard byte cap and content-type check.

    ``POST /api/photo-search`` used ``shutil.copyfileobj`` straight into a
    tempfile: unlimited size, no content-type validation, a temp file that was
    never deleted. Use this instead.

    Args:
        source: Async or sync file-like object (``UploadFile.file`` qualifies).
        max_bytes: Cap; defaults to :data:`MAX_UPLOAD_BYTES`.
        allowed_content_types: Content-type allowlist; ``None`` accepts any.
        filename: Optional client filename (sanitized before echoing back).
        chunk_size: Read granularity.

    Returns:
        ``{"ok": True, "data": bytes, "filename": str, "bytes": int}`` or
        ``{"ok": False, "error": str}``. Never raises.
    """
    cap = int(max_bytes or MAX_UPLOAD_BYTES)
    try:
        declared = getattr(source, "content_type", None)
    except Exception:  # noqa: BLE001
        declared = None
    if allowed_content_types is not None and declared:
        if not _content_type_allowed(declared, allowed_content_types):
            return {"ok": False, "error": f"content-type not allowed: {declared}"}
    try:
        import anyio  # noqa: F401 - used only when present

        data = await _read_async(source, cap, chunk_size)
    except ImportError:
        data = _read_sync(source, cap, chunk_size)
    except Exception as exc:  # noqa: BLE001 - never break the request
        return {"ok": False, "error": f"upload read failed: {exc}"}
    if len(data) > cap:
        return {"ok": False, "error": f"upload exceeds {cap} bytes"}
    return {
        "ok": True,
        "data": data,
        "bytes": len(data),
        "filename": sanitize_filename(filename) if filename else "",
    }


async def _read_async(source: Any, cap: int, chunk_size: int) -> bytes:
    """Read at most ``cap`` bytes from an async or sync file object."""
    chunks: list[bytes] = []
    total = 0
    reader = getattr(source, "read", None)
    if reader is None:
        return b""
    while total <= cap:
        chunk = reader(chunk_size)
        if hasattr(chunk, "__await__"):
            chunk = await chunk
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)[: cap + 1]


def _read_sync(source: Any, cap: int, chunk_size: int) -> bytes:
    """Blocking read of at most ``cap`` bytes, used when anyio is absent."""
    chunks: list[bytes] = []
    total = 0
    while total <= cap:
        chunk = source.read(chunk_size)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)[: cap + 1]


# ── Content types ────────────────────────────────────────────────────────────

def _content_type_allowed(content_type: str, allowed: Sequence[str] | None) -> bool:
    """Match a Content-Type header against a prefix/subtype allowlist.

    Args:
        content_type: The raw ``Content-Type`` header value.
        allowed: Allowlist of prefixes. ``None`` *or an empty sequence* means
            "no restriction" — ``None`` selects the module default upstream,
            while ``()`` is an explicit opt-out for a vetted caller.

    Returns:
        ``True`` when the content type is acceptable.
    """
    if not allowed:
        return True
    ct = str(content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        return True  # header absent — allowlist does not apply
    for entry in allowed:
        want = str(entry).split(";", 1)[0].strip().lower()
        if want == ct or ct.startswith(want):
            return True
    return False


# ── safe_fetch ───────────────────────────────────────────────────────────────

def _err(message: str, url: str = "", status: int = 0, **extra: Any) -> dict[str, Any]:
    """Build the structured failure shape every caller expects.

    Args:
        message: Human-readable reason (never contains a secret).
        url: The URL that failed.
        status: HTTP status when one was received, else ``0``.
        **extra: Additional keys merged into the result.

    Returns:
        A ``{"ok": False, "error": ...}`` dict.
    """
    out: dict[str, Any] = {
        "ok": False,
        "error": message,
        "status_code": status,
        "final_url": url,
        "headers": {},
        "text": "",
        "content": b"",
        "truncated": False,
    }
    out.update(extra)
    return out


def _strip_hop_secrets(headers: Mapping[str, str], previous_host: str, next_host: str) -> dict[str, str]:
    """Drop credential headers when a redirect crosses origins."""
    if previous_host == next_host:
        return dict(headers)
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _SENSITIVE_HOP_HEADERS
    }


async def safe_fetch(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 20.0,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json: Any = None,          # noqa: A002 - name fixed by connectors/net.py
    data: Any = None,
    files: Any = None,
    content: bytes | None = None,
    max_bytes: int | None = None,
    allow_private: bool | None = None,
    allow_redirects: bool = True,
    max_redirects: int = MAX_REDIRECTS,
    allowed_content_types: Sequence[str] | None = None,
    user_agent: str = DEFAULT_UA,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Fetch a URL with SSRF protection, a byte cap and a content-type allowlist.

    This is the single hardened outbound path for Signal-OS. It validates the
    URL, resolves and checks every address, follows redirects **manually** so
    each hop is re-validated (a public URL cannot bounce the crawler onto
    ``169.254.169.254``), streams the body with a hard cap, and sets an honest
    User-Agent.

    Args:
        url: Absolute ``http``/``https`` URL.
        method: HTTP verb.
        timeout: Per-hop timeout in seconds.
        headers: Extra request headers. ``User-Agent`` is always set.
        params: Query-string parameters.
        json: JSON request body.
        data: Form body.
        files: Multipart files.
        content: Raw request body.
        max_bytes: Response cap; defaults to :data:`MAX_DOWNLOAD_BYTES`.
        allow_private: Force-allow private targets (``None`` reads the env).
        allow_redirects: Whether to follow 3xx at all.
        max_redirects: Hop limit.
        allowed_content_types: Content-type allowlist. ``None`` (the default)
            means "use :data:`ALLOWED_CONTENT_TYPES`"; pass ``()`` to disable
            the check entirely for a caller that has vetted its own source.
        user_agent: Override for the default User-Agent.
        proxy: Optional proxy URL (OPSEC/Tor).

    Returns:
        ``{"ok": bool, "status_code": int, "headers": dict, "text": str,
        "content": bytes, "truncated": bool, "final_url": str,
        "error": str|None}``. Network and SSRF failures are returned as
        ``{"ok": False, "error": ...}`` — this function does not raise.
    """
    import httpx  # lazy: keeps import time and hard-dependency risk low

    cap = int(max_bytes or MAX_DOWNLOAD_BYTES)
    if allowed_content_types is None:
        allowed_content_types = ALLOWED_CONTENT_TYPES
    if allow_private is None:
        allow_private = private_network_allowed()
    verb = (method or "GET").upper()

    current = str(url or "")
    hops = 0
    while True:
        if not is_safe_url(current, allow_private=allow_private):
            return _err(f"unsafe url: {current[:120]}", url=current)
        try:
            parsed = urlparse(current)
            host = _normalize_host(parsed.hostname or "")
        except ValueError:
            return _err(f"malformed url: {current[:120]}", url=current)
        try:
            await assert_public_host(host, allow_private=allow_private)
        except SSRFError as exc:
            return _err(str(exc), url=current)
        except Exception as exc:  # noqa: BLE001 - defensive
            return _err(f"host validation failed: {exc}", url=current)

        req_headers = {"user-agent": user_agent, "accept": "*/*"}
        req_headers.update(dict(headers or {}))
        req_headers.setdefault("user-agent", user_agent)

        transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None
        client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, verify=True, transport=transport
        )
        try:
            async with client.stream(
                verb, current, headers=req_headers, params=params,
                json=json, data=data, files=files, content=content,
            ) as resp:
                if allow_redirects and 300 <= resp.status_code < 400:
                    location = resp.headers.get("location")
                    if not location:
                        return _err(
                            f"HTTP {resp.status_code} redirect without location",
                            url=current, status=resp.status_code,
                        )
                    if hops >= max_redirects:
                        return _err("too many redirects", url=current,
                                    status=resp.status_code)
                    nxt = urljoin(current, location)
                    hops += 1
                    try:
                        nxt_host = _normalize_host(urlparse(nxt).hostname or "")
                    except ValueError:
                        return _err("malformed redirect target", url=current,
                                    status=resp.status_code)
                    req_headers = _strip_hop_secrets(req_headers, host, nxt_host)
                    # A redirect that changes host must not carry the body.
                    if nxt_host != host:
                        params = json = data = files = content = None
                        if verb not in ("GET", "HEAD"):
                            verb = "GET"
                    current = nxt
                    continue

                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                ctype = resp.headers.get("content-type", "")
                if not _content_type_allowed(ctype, allowed_content_types):
                    return _err(
                        f"content-type not allowed: {ctype or '(none)'}",
                        url=current, status=resp.status_code, headers=resp_headers,
                    )
                declared = resp.headers.get("content-length")
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
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                body = b"".join(chunks)
                ok = 200 <= resp.status_code < 300
                return {
                    "ok": ok,
                    "status_code": resp.status_code,
                    "headers": resp_headers,
                    "content": body,
                    "text": body.decode(resp.encoding or "utf-8", "replace"),
                    "truncated": truncated,
                    "final_url": str(resp.url),
                    "content_type": ctype,
                    "content_length": int(declared) if (declared or "").isdigit() else None,
                    "redirects": hops,
                    "error": None if ok else f"HTTP {resp.status_code}",
                }
        except SSRFError as exc:  # pragma: no cover - re-raised by guard above
            return _err(str(exc), url=current)
        except Exception as exc:  # noqa: BLE001 - network failures are results
            return _err(f"{type(exc).__name__}: {exc}", url=current)
        finally:
            await client.aclose()


# ── Request helpers ──────────────────────────────────────────────────────────

def client_ip(request: Any) -> str:
    """Best-effort client IP for audit logging.

    ``X-Forwarded-For`` is only trusted when ``TRUST_PROXY_HEADERS=true``,
    otherwise any client could forge its own log entry.

    Args:
        request: A Starlette/FastAPI request.

    Returns:
        The client IP string, or ``"unknown"``.
    """
    try:
        if _env_flag("TRUST_PROXY_HEADERS", False):
            forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0]
            if forwarded.strip():
                return forwarded.strip()[:64]
        return (request.client.host if request.client else "unknown")[:64]
    except Exception:  # noqa: BLE001
        return "unknown"


def rate_limit_headers(principal: str | Mapping[str, Any], scope: str) -> dict[str, str]:
    """Build the ``X-RateLimit-*`` response headers for a request.

    Thin re-export of :func:`rate_limit.rate_limit_headers` so the HTTP layer
    only needs to import ``security``.

    Args:
        principal: Principal id (or a principal dict from :mod:`auth`).
        scope: Rate-limit scope (``investigate``, ``search``, ``read``, ...).

    Returns:
        Header dict; empty when rate limiting is unavailable.
    """
    try:
        from rate_limit import rate_limit_headers as _impl  # lazy: avoids a cycle
    except Exception:  # noqa: BLE001
        return {}
    pid = principal.get("id") if isinstance(principal, Mapping) else principal
    return _impl(str(pid or "anonymous"), scope)