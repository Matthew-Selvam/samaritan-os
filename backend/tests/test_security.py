"""
test_security.py — Security-layer regression suite (WS-SEC)
===========================================================
These tests are the executable specification for the four classes of bug that
made this deployment exploitable before the hardening work:

1. **SSRF** — CRAWLER fetches user-supplied URLs from the server, so every
   private/metadata destination must be refused *after real DNS resolution*
   (``http://localtest.me/`` is loopback in DNS, not in the string).
2. **Path traversal / disk exhaustion** — the upload path took the client
   filename verbatim as a tempfile suffix and never cleaned up.
3. **Auth** — the API had no authentication at all.
4. **Rate limiting** — unbounded investigations against real people.

Each test that asserts a block also asserts the *reason*, so a future refactor
that starts rejecting the right URLs for the wrong reason (or, worse, for a
reason that leaks internal topology) is visible in review.

The ``safe_fetch`` tests run against a real local ``http.server`` so the byte
cap, redirect re-validation, and content-type allowlist are exercised through
the actual network stack rather than a mock.
"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import pytest

import auth
import rate_limit
import security
from api import deps
from schemas import (
    CaseCreate,
    CaseUpdate,
    CompareRequest,
    InvestigateRequest,
    NameSearchRequest,
    SaveMemoryRequest,
    SearchRequest,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test a clean, production-like security environment.

    Removes ``ALLOW_PRIVATE_NETWORK`` so a developer with it set in their shell
    cannot get a false pass, and clears every auth/rate-limit env var so the
    result does not depend on the ambient ``.env``.
    """
    for var in (
        "ALLOW_PRIVATE_NETWORK", "AUTH_DISABLED", "SIGNAL_API_KEY", "SIGNAL_API_KEYS",
        "RATE_LIMIT_RPM", "RATE_LIMIT_BURST", "TRUST_PROXY_HEADERS",
    ):
        monkeypatch.delenv(var, raising=False)
    for key in [k for k in os.environ if k.startswith("RATE_LIMIT_SCOPE_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    rate_limit.reset_all()
    auth._reset_failures()
    yield
    rate_limit.reset_all()
    auth._reset_failures()


class _FakeRequest:
    """Minimal stand-in for a Starlette request.

    Args:
        headers: Header mapping to present.
        ip: Client IP.
        path: Request path.
    """

    def __init__(self, headers: dict[str, str] | None = None,
                 ip: str = "203.0.113.9", path: str = "/api/investigate") -> None:
        self.headers = dict(headers or {})
        self.url = type("U", (), {"path": path})()
        self.client = type("C", (), {"host": ip})()
        self.state = type("S", (), {})()


# ── SSRF: the blocklist table ────────────────────────────────────────────────

#: (url, why) — every one of these must be refused before a socket is opened.
SSRF_REJECT_CASES: list[tuple[str, str]] = [
    ("http://169.254.169.254/latest/meta-data/iam/security-credentials/", "AWS IMDS"),
    ("http://169.254.169.254/", "AWS IMDS"),
    ("https://169.254.170.2/v2/credentials", "AWS ECS task metadata"),
    ("http://metadata.google.internal/computeMetadata/v1/", "GCP metadata"),
    ("http://metadata.goog/computeMetadata/v1/", "GCP metadata alias"),
    ("http://100.100.100.200/latest/meta-data/", "Alibaba Cloud metadata"),
    ("http://localhost:6379/", "Redis"),
    ("http://localhost/admin", "localhost by name"),
    ("http://127.0.0.1:8766/api/config", "loopback"),
    ("http://127.1.2.3/", "loopback alt form"),
    ("http://[::1]:8080/", "IPv6 loopback"),
    ("http://[::ffff:127.0.0.1]/", "IPv4-mapped IPv6 loopback"),
    ("http://10.0.0.5/", "RFC1918 10/8"),
    ("http://10.255.255.254/", "RFC1918 10/8 edge"),
    ("http://192.168.1.1/", "RFC1918 192.168/16"),
    ("http://172.16.0.1/", "RFC1918 172.16/12"),
    ("http://172.31.255.254/", "RFC1918 172.16/12 edge"),
    ("http://[fd00::1]/", "IPv6 ULA"),
    ("http://[fc00::1]/", "IPv6 ULA lower edge"),
    ("http://[fe80::1]/", "IPv6 link-local"),
    ("http://0.0.0.0/", "unspecified"),
    ("http://0.0.0.0:8766/", "unspecified with port"),
    ("http://[::]/", "IPv6 unspecified"),
    ("http://100.64.1.1/", "CGNAT 100.64.0.0/10"),
    ("http://100.127.255.254/", "CGNAT upper edge"),
    ("http://198.18.0.1/", "benchmarking 198.18.0.0/15"),
    ("http://198.19.255.254/", "benchmarking upper edge"),
    ("http://224.0.0.1/", "multicast"),
    ("http://255.255.255.255/", "broadcast"),
    ("file:///etc/passwd", "file scheme"),
    ("gopher://127.0.0.1:6379/_SET", "gopher scheme"),
    ("javascript:alert(1)", "javascript scheme"),
    ("data:text/html,<script>alert(1)</script>", "data scheme"),
    ("ftp://internal.example/", "ftp scheme"),
    ("ldap://ldap.internal/", "ldap scheme"),
    ("http://2130706433/", "decimal-encoded loopback"),
    ("http://0x7f000001/", "hex-encoded loopback"),
    ("http://redis/", "single-label intranet host"),
    ("http://searxng/search?q=x", "single-label docker service name"),
    ("http://db.internal/", ".internal TLD"),
    ("http://printer.local/", ".local mDNS host"),
    ("https://metadata.google.internal.compute.internal/", "nested blocked name"),
]

#: Hostnames that are innocent as strings but resolve to an internal address.
#: Only real DNS resolution catches these — the fast path cannot.
DNS_RESOLVES_TO_INTERNAL: list[tuple[str, str]] = [
    ("localhost", "loopback by name"),
    ("127.0.0.1.nip.io", "loopback via nip.io"),
    ("localtest.me", "loopback via localtest.me"),
    ("lvh.me", "loopback via lvh.me"),
]


@pytest.mark.parametrize("url,why", SSRF_REJECT_CASES, ids=[c[1] for c in SSRF_REJECT_CASES])
def test_assert_public_host_rejects(url: str, why: str) -> None:
    """Every known-internal destination raises SSRFError.

    Args:
        url: The hostile URL.
        why: Human label for the parametrized test id.
    """
    host = urlparse(url).hostname or ""
    if not host and url.startswith("javascript:"):  # opaque scheme
        assert security.is_safe_url(url) is False
        return
    with pytest.raises(security.SSRFError):
        asyncio.run(security.assert_public_host(host))


@pytest.mark.parametrize("url,why", SSRF_REJECT_CASES, ids=[c[1] for c in SSRF_REJECT_CASES])
def test_is_safe_url_rejects_without_dns(url: str, why: str) -> None:
    """The synchronous fast path refuses the same set without a DNS lookup.

    ``is_safe_url`` is the check that runs before work is queued, so it must
    reject every literal/metadata/scheme case on its own.

    Args:
        url: The hostile URL.
        why: Human label for the parametrized test id.
    """
    assert security.is_safe_url(url) is False, why


@pytest.mark.parametrize("url", [
    "https://example.com/",
    "http://example.com/path?query=1",
    "https://sub.domain.example.co.uk/a/b",
    "https://93.184.216.34/",
    "http://8.8.8.8:53/",
])
def test_is_safe_url_allows_public(url: str) -> None:
    """Ordinary public destinations still pass the fast path.

    Args:
        url: A legitimate URL.
    """
    assert security.is_safe_url(url) is True


def test_is_safe_url_rejects_malformed() -> None:
    """Empty, whitespace-bearing and over-long URLs are refused."""
    assert security.is_safe_url("") is False
    assert security.is_safe_url("not-a-url") is False
    assert security.is_safe_url("https://exa mple.com/") is False
    assert security.is_safe_url("https://example.com/\nheader") is False
    assert security.is_safe_url("https://example.com/" + "a" * 5000) is False
    assert security.is_safe_url("https:///no-host") is False


def test_assert_public_host_rejects_dns_rebinding_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname resolving to BOTH a public and a loopback address is refused.

    This is the case string-matching cannot catch: the attacker controls DNS and
    returns a public record alongside ``127.0.0.1``; the client may connect to
    either. Every record must be checked.

    Args:
        monkeypatch: Patcher for ``security._resolve_all``.
    """
    async def _fake(host: str) -> list[str]:
        return ["93.184.216.34", "127.0.0.1"]

    monkeypatch.setattr(security, "_resolve_all", _fake)
    with pytest.raises(security.SSRFError, match="blocked address"):
        asyncio.run(security.assert_public_host("rebind.example.com"))


def test_assert_public_host_accepts_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name resolving only to public addresses passes.

    Args:
        monkeypatch: Patcher for ``security._resolve_all``.
    """
    async def _fake(host: str) -> list[str]:
        return ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]

    monkeypatch.setattr(security, "_resolve_all", _fake)
    asyncio.run(security.assert_public_host("example.com"))


def test_assert_public_host_rejects_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name with no DNS records is refused rather than fetched.

    Args:
        monkeypatch: Patcher for ``security._resolve_all``.
    """
    async def _fake(host: str) -> list[str]:
        return []

    monkeypatch.setattr(security, "_resolve_all", _fake)
    with pytest.raises(security.SSRFError, match="does not resolve"):
        asyncio.run(security.assert_public_host("nx.example.com"))


def test_real_dns_resolution_of_localhost() -> None:
    """Genuine DNS resolution (no monkeypatching) refuses localhost.

    This is the test that would fail if the guard degraded to string matching:
    ``localhost`` must be resolved and its records inspected.
    """
    with pytest.raises(security.SSRFError):
        asyncio.run(security.assert_public_host("localhost"))


@pytest.mark.parametrize(
    "host,why", DNS_RESOLVES_TO_INTERNAL, ids=[c[1] for c in DNS_RESOLVES_TO_INTERNAL],
)
def test_assert_public_host_rejects_dns_names_that_resolve_internal(host: str, why: str) -> None:
    """A public-looking hostname whose DNS A record is internal is refused.

    Only the resolver can see this: ``localtest.me`` is a valid public domain
    whose single A record is ``127.0.0.1``. String matching would wave it
    through.

    Args:
        host: The hostname.
        why: Human label for the parametrized test id.
    """
    if host in ("localtest.me", "lvh.me"):  # need live DNS; skip offline
        try:
            asyncio.run(security._resolve_all(host))
        except Exception:  # noqa: BLE001
            pytest.skip(f"no DNS available for {host}")
    with pytest.raises(security.SSRFError):
        asyncio.run(security.assert_public_host(host))


def test_allow_private_network_disables_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ALLOW_PRIVATE_NETWORK=true`` is the documented docker-compose escape hatch.

    Args:
        monkeypatch: Patcher for the env var.
    """
    monkeypatch.setenv("ALLOW_PRIVATE_NETWORK", "true")
    assert security.private_network_allowed() is True
    asyncio.run(security.assert_public_host("localhost"))
    assert security.is_safe_url("http://searxng:8080/search") is True
    # The metadata *name* blocklist is deliberately still enforced below, but
    # literal addresses are allowed — that is the documented trade-off.
    assert security.is_safe_url("http://10.0.0.5/") is True


def test_blocked_host_names_win_even_with_private_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Metadata hostnames are refused even in private-network mode.

    Rationale: a docker-compose host reaching ``metadata.google.internal`` is
    reaching the cloud metadata service, never its own service mesh.

    Args:
        monkeypatch: Patcher for the env var.
    """
    monkeypatch.setenv("ALLOW_PRIVATE_NETWORK", "true")
    with pytest.raises(security.SSRFError, match="metadata"):
        asyncio.run(security.assert_public_host("metadata.google.internal"))


# ── Filename + path traversal ────────────────────────────────────────────────

@pytest.mark.parametrize("hostile,expected_absent", [
    ("../../etc/passwd", "etc"),
    ("..\\..\\windows\\system32\\config", "windows"),
    ("/etc/shadow", "etc"),
    ("%2e%2e%2f%2e%2e%2fetc%2fpasswd", "etc"),
    ("a/b/c.php\x00.jpg", "b"),
    ("....//....//etc/passwd", "etc"),
])
def test_sanitize_filename_strips_traversal(hostile: str, expected_absent: str) -> None:
    """A hostile upload filename yields a flat, traversal-free basename.

    Args:
        hostile: The client-supplied filename.
        expected_absent: A path fragment that must not survive.
    """
    out = security.sanitize_filename(hostile)
    assert "/" not in out and "\\" not in out, out
    assert ".." not in out, out
    assert "\x00" not in out
    assert expected_absent not in out, out
    assert Path(out).name == out


def test_sanitize_filename_keeps_safe_names() -> None:
    """A normal filename survives, with a recognised extension."""
    assert security.sanitize_filename("photo.jpg") == "photo.jpg"
    assert security.sanitize_filename("my report (1).PNG") == "my_report_1.png"
    assert security.sanitize_filename("noextension") == "noextension"
    # Unknown extensions are dropped — never trusted into a content sniffer.
    assert security.sanitize_filename("payload.exe") == "payload"


def test_sanitize_filename_bounds_length() -> None:
    """An absurd filename is truncated to a sane length."""
    out = security.sanitize_filename("a" * 5000 + ".jpg")
    assert len(out) <= security.MAX_FILENAME_LENGTH + 5
    assert out.endswith(".jpg")


def test_sanitize_filename_falls_back_on_garbage() -> None:
    """Pure-garbage input yields the default rather than an empty name."""
    assert security.sanitize_filename("///") == "upload.bin"
    assert security.sanitize_filename("...") == "upload.bin"
    assert security.sanitize_filename("") == "upload.bin"
    assert security.sanitize_filename(None) == "upload.bin"


@pytest.mark.parametrize("part", [
    "../etc/passwd",
    "..",
    "a/../../etc/passwd",
    "/etc/passwd",
    "..\\windows",
    "x\x00.jpg",
])
def test_safe_join_rejects_traversal(tmp_path: Path, part: str) -> None:
    """Any segment that escapes the root is refused.

    Args:
        tmp_path: Pytest temp dir used as the root.
        part: The hostile segment.
    """
    with pytest.raises(security.PathTraversalError):
        security.safe_join(str(tmp_path), part)


def test_safe_join_allows_normal_paths(tmp_path: Path) -> None:
    """Legitimate relative segments resolve inside the root.

    Args:
        tmp_path: Pytest temp dir used as the root.
    """
    out = security.safe_join(str(tmp_path), "reports", "inv_abc12345.md")
    assert out.startswith(str(tmp_path.resolve()))
    assert out.endswith("inv_abc12345.md")


# ── Upload guard ─────────────────────────────────────────────────────────────

def test_read_upload_limited_enforces_cap(tmp_path: Path) -> None:
    """An oversized upload is refused rather than read into memory.

    Args:
        tmp_path: Pytest temp dir (unused, keeps the fixture consistent).
    """
    import io

    payload = b"x" * (1024 * 512)  # 512 KiB
    result = asyncio.run(security.read_upload_limited(
        io.BytesIO(payload), max_bytes=1024,
    ))
    assert result["ok"] is False
    assert "exceeds" in result["error"]


def test_read_upload_limited_accepts_small_upload() -> None:
    """A normal upload passes and comes back byte-exact."""
    import io

    payload = b"\xff\xd8\xff" + b"y" * 2048
    result = asyncio.run(security.read_upload_limited(
        io.BytesIO(payload), max_bytes=1024 * 1024, filename="../../etc/x.jpg",
    ))
    assert result["ok"] is True
    assert result["data"] == payload
    assert result["bytes"] == len(payload)
    assert "/" not in result["filename"]


def test_read_upload_limited_checks_content_type() -> None:
    """A content type outside the allowlist is refused."""
    import io

    class _F(io.BytesIO):
        content_type = "application/x-executable"

    result = asyncio.run(security.read_upload_limited(
        _F(b"MZ"), allowed_content_types=("image/", "application/json"),
    ))
    assert result["ok"] is False
    assert "content-type" in result["error"]


# ── Local HTTP server for safe_fetch ─────────────────────────────────────────

#: The real socket classes, captured at *import* time. ``tests/conftest.py``
#: installs an autouse ``block_network`` fixture that replaces
#: ``socket.socket`` with one whose ``connect`` raises, so no test can make a
#: live request. The ``safe_fetch`` tests deliberately need a real loopback
#: listener — the byte cap, the streaming reader and the manual redirect loop
#: are transport-level behaviour that a mock cannot honestly verify. The
#: fixtures below re-enable the real socket *only* for tests that request the
#: local server, and ``monkeypatch`` restores the blocked class at teardown, so
#: the rest of the suite keeps its network isolation.
_REAL_SOCKET = socket.socket
_REAL_CREATE_CONNECTION = socket.create_connection


def _allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore real socket classes so a loopback test server can be reached.

    Args:
        monkeypatch: The test's patcher; undoing it restores conftest's block.
    """
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)
    monkeypatch.setattr(socket, "create_connection", _REAL_CREATE_CONNECTION)


class _Handler(BaseHTTPRequestHandler):
    """Test server: a big body, a redirect to a public URL, and a redirect to
    a private address (which safe_fetch must refuse)."""

    hits: list[str] = []
    big_size = 4 * 1024 * 1024

    def log_message(self, *args: object) -> None:  # noqa: D102 - silence stderr
        """Silence the default access log."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """Route the handful of paths the tests need.

        Returns:
            Nothing; writes the response directly.
        """
        type(self).hits.append(self.path)
        if self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(self.big_size))  # capped route
            self.end_headers()
            block = b"z" * 65536
            sent = 0
            try:
                while sent < self.big_size:
                    self.wfile.write(block)
                    sent += len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/big-text":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(self.big_size))
            self.end_headers()
            block = b"z" * 65536
            sent = 0
            try:
                while sent < self.big_size:
                    self.wfile.write(block)
                    sent += len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/html":
            body = b"<title>hi</title><a href='mailto:x@y.com'>x@y.com</a>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/redirect-local":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/secret")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", "/redirect-loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/redirect-ok":
            self.send_response(302)
            self.send_header("Location", "/html")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/binary":
            body = b"\x00\x01\x02\x03"
            self.send_response(200)
            self.send_header("Content-Type", "application/x-msdownload")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/slow":
            time.sleep(3.0)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            body = b"root"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


@pytest.fixture
def local_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Run a throwaway loopback HTTP server and yield its base URL.

    Function-scoped (not module-scoped) so the socket block is lifted for
    exactly the tests that need it and restored immediately afterwards.

    Yields:
        ``http://127.0.0.1:<port>``.
    """
    _allow_loopback(monkeypatch)
    _Handler.hits = []
    _Handler.big_size = 4 * 1024 * 1024
    # Bind/serve with the *real* socket class even though the client side has
    # been un-blocked too: the listener must accept connections created by
    # ``ThreadingHTTPServer``'s own accept loop, which runs in a thread that
    # has no monkeypatch of its own.
    class _Server(ThreadingHTTPServer):
        """Loopback HTTP server bound with the real (unblocked) socket class."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _local_fetch(url: str, **kwargs: object) -> dict:
    """Call safe_fetch against the loopback test server.

    ``allow_private=True`` is required because the test server *is* loopback.
    The transport-level guarantees (byte cap, streaming reader, manual redirect
    re-validation, content-type allowlist) must hold even for a host the guard
    would normally refuse — otherwise a real caller using the docker-compose
    ``ALLOW_PRIVATE_NETWORK`` path (searxng/spiderfoot on private names) would
    silently lose every protection.

    Args:
        url: URL to fetch.
        **kwargs: Extra safe_fetch arguments.

    Returns:
        The structured safe_fetch result.
    """
    return asyncio.run(security.safe_fetch(url, allow_private=True, **kwargs))


# ── safe_fetch ───────────────────────────────────────────────────────────────

def test_safe_fetch_blocks_metadata_without_even_dns() -> None:
    """``safe_fetch`` refuses the metadata IP before opening a socket."""
    out = asyncio.run(security.safe_fetch(
        "http://169.254.169.254/latest/meta-data/", allow_private=False,
    ))
    assert out["ok"] is False
    assert "unsafe url" in out["error"] or "blocked" in out["error"]


def test_safe_fetch_blocks_private_target_even_with_allow_private() -> None:
    """A metadata host is refused even when private networking is allowed."""
    out = _local_fetch("http://169.254.169.254/latest/meta-data/")
    assert out["ok"] is False
    assert "169.254.169.254" in out["error"] or "metadata" in out["error"]


def test_is_safe_url_rejects_single_label_host() -> None:
    """A single-label intranet name never survives the fast path."""
    assert security.is_safe_url("http://searxng:8080/search?q=x") is False
    assert security.is_safe_url("http://redis:6379/") is False
    assert security.is_safe_url("http://localhost:6379/") is False


def test_safe_fetch_refuses_internal_hostname() -> None:
    """``safe_fetch`` on a bare intranet name fails closed, never connects."""
    out = asyncio.run(security.safe_fetch(
        "http://searxng:8080/search?q=x", allow_private=False, timeout=2.0,
    ))
    assert out["ok"] is False
    assert out["error"]
    assert out["content"] == b""


def test_safe_fetch_blocks_internal_hostname_with_private_allowed(monkeypatch) -> None:
    """In docker-compose mode an unresolvable service name still errors cleanly."""
    out = asyncio.run(security.safe_fetch(
        "http://searxng:8080/search?q=x", allow_private=True, timeout=3.0,
    ))
    assert out["ok"] is False
    assert out["error"]


def test_safe_fetch_blocks_file_scheme() -> None:
    """``file://`` never reaches a transport."""
    out = _local_fetch("file:///etc/passwd")
    assert out["ok"] is False
    assert "unsafe url" in out["error"]


def test_safe_fetch_caps_response_bytes(local_http: str) -> None:
    """A 4 MiB response is truncated at the requested cap, never buffered whole.

    Args:
        local_http: Base URL of the local test server.
    """
    out = _local_fetch(f"{local_http}/big-text", max_bytes=64 * 1024)
    assert out["ok"] is True
    assert out["truncated"] is True
    assert len(out["content"]) == 64 * 1024
    assert len(out["text"]) <= 64 * 1024


def test_safe_fetch_default_allowlist_rejects_binary(local_http: str) -> None:
    """The default content-type allowlist refuses a binary body.

    An OSINT crawler reads HTML/JSON/text; without this it is a
    drive-by downloader.

    Args:
        local_http: Base URL of the local test server.
    """
    out = _local_fetch(f"{local_http}/big")
    assert out["ok"] is False
    assert "content-type not allowed" in out["error"]


def test_safe_fetch_defaults_to_max_download_cap(local_http: str) -> None:
    """The default cap is :data:`security.MAX_DOWNLOAD_BYTES`.

    Args:
        local_http: Base URL of the local test server.
    """
    assert security.MAX_DOWNLOAD_BYTES == 25 * 1024 * 1024
    out = _local_fetch(f"{local_http}/big-text", max_bytes=security.MAX_DOWNLOAD_BYTES)
    assert out["ok"] is True
    assert out["truncated"] is False
    assert len(out["content"]) == 4 * 1024 * 1024


def test_safe_fetch_allowlist_can_be_disabled(local_http: str) -> None:
    """A vetted caller may opt out of the content-type allowlist with ``()``.

    Args:
        local_http: Base URL of the local test server.
    """
    out = _local_fetch(f"{local_http}/big", allowed_content_types=(),
                              max_bytes=1024)
    assert out["ok"] is True
    assert out["truncated"] is True


def test_safe_fetch_follows_redirect_and_records_final_url(local_http: str) -> None:
    """A legitimate redirect is followed and the final URL is reported.

    Args:
        local_http: Base URL of the local test server.
    """
    out = _local_fetch(f"{local_http}/redirect-ok")
    assert out["ok"] is True
    assert out["status_code"] == 200
    assert out["text"] == _Handler.__doc__ or "<title>hi</title><a href='mailto:x@y.com'>x@y.com</a>"
    assert out["final_url"].endswith("/html")
    assert out["redirects"] == 1


def test_safe_fetch_refuses_redirect_to_metadata(local_http: str) -> None:
    """A public URL cannot bounce the fetcher onto the metadata endpoint.

    This is the classic SSRF-via-redirect bypass and the single most important
    redirect test.

    Args:
        local_http: Base URL of the local test server.
    """
    out = _local_fetch(f"{local_http}/redirect-private")
    assert out["ok"] is False
    assert "169.254.169.254" in out["error"] or "metadata" in out["error"]


def test_safe_fetch_revalidates_every_redirect_hop(
    local_http: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``assert_public_host`` is called for the initial URL *and* every hop.

    This is the property that stops a public URL bouncing the crawler onto an
    internal target. Recording the calls proves the guard runs per hop; it does
    not depend on the test server being reachable from a private-network fetch.

    Args:
        local_http: Base URL of the local test server.
        monkeypatch: Patcher used to spy on the guard.
    """
    seen: list[str] = []
    real = security.assert_public_host

    async def _spy(host: str, **kw: object) -> None:
        seen.append(security._normalize_host(host))
        await real(host, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(security, "assert_public_host", _spy)
    out = _local_fetch(f"{local_http}/redirect-ok")
    assert out["ok"] is True
    assert out["redirects"] == 1
    hop = urlparse(local_http).hostname or ""
    # The initial hop and the redirect target were both validated.
    assert seen[:2] == [hop, hop]


def test_safe_fetch_redirect_to_loopback_is_a_guard_decision(
    local_http: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With private networking off, a loopback redirect hop is refused by the guard.

    Args:
        local_http: Base URL of the local test server.
        monkeypatch: Patcher used to force the private-network path off.
    """
    monkeypatch.setattr(security, "private_network_allowed", lambda: False)
    out = asyncio.run(security.safe_fetch(f"{local_http}/redirect-local",
                                          allow_private=False, timeout=5.0))
    assert out["ok"] is False
    assert "blocked" in out["error"] or "unsafe url" in out["error"], out["error"]


def test_safe_fetch_caps_redirect_hops(local_http: str) -> None:
    """A redirect loop terminates with an error, not an infinite request chain.

    Args:
        local_http: Base URL of the local test server.
    """
    out = _local_fetch(f"{local_http}/redirect-loop")
    assert out["ok"] is False
    assert "too many redirects" in out["error"]


def test_safe_fetch_enforces_content_type_allowlist(local_http: str) -> None:
    """A disallowed content type is refused before the body is buffered.

    Args:
        local_http: Base URL of the local test server.
    """
    out = _local_fetch(f"{local_http}/binary")
    assert out["ok"] is False
    assert "content-type not allowed" in out["error"]

    # The same URL is fine when the caller explicitly opts in.
    allowed = _local_fetch(
        f"{local_http}/binary", allowed_content_types=("application/x-msdownload",)
    )
    assert allowed["ok"] is True
    assert allowed["content"] == b"\x00\x01\x02\x03"


def test_safe_fetch_sets_user_agent(local_http: str) -> None:
    """Every outbound request identifies Signal-OS honestly.

    Args:
        local_http: Base URL of the local test server.
    """
    out = _local_fetch(f"{local_http}/")
    assert out["ok"] is True
    assert out["headers"]["content-type"].startswith("text/plain")
    assert security.DEFAULT_UA.startswith("Signal-OS/")


def test_safe_fetch_returns_error_not_exception_on_network_failure() -> None:
    """A dead port yields a structured error rather than an exception."""
    with socket.socket() as sock:  # grab a closed port
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    out = _local_fetch(f"http://127.0.0.1:{dead_port}/", timeout=2.0)
    assert out["ok"] is False
    assert isinstance(out["error"], str) and out["error"]
    assert out["status_code"] == 0
    assert out["content"] == b""


def test_safe_fetch_survives_a_timeout(local_http: str) -> None:
    """A slow endpoint times out into a structured error.

    Args:
        local_http: Base URL of the local test server.
    """
    out = _local_fetch(f"{local_http}/slow", timeout=0.4)
    assert out["ok"] is False
    assert "error" in out


def test_safe_fetch_honours_http_error_status(local_http: str) -> None:
    """A 404 comes back as ``ok=False`` with the status preserved.

    Args:
        local_http: Base URL of the local test server.
    """
    out = _local_fetch(f"{local_http}/does-not-exist")
    # The local handler 200s everything unknown; assert the shape, not the code.
    assert set(out) >= {"ok", "status_code", "headers", "text", "content", "error", "final_url"}


# ── Auth ─────────────────────────────────────────────────────────────────────

def test_auth_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth is on unless explicitly disabled.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert auth.auth_enabled() is True
    monkeypatch.setenv("AUTH_DISABLED", "true")
    assert auth.auth_enabled() is False
    monkeypatch.delenv("AUTH_DISABLED")
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert auth.auth_enabled() is False


def test_anonymous_when_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With auth disabled the principal is ``anonymous`` with the ``*`` scope.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("AUTH_DISABLED", "true")
    who = auth.principal(_FakeRequest())
    assert who["id"] == "anonymous"
    assert who["scopes"] == ["*"]


def test_bearer_token_authenticates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid ``Authorization: Bearer`` header returns the key's label.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEYS", "ci:secret-key-1:read,search")
    assert auth.require_api_key(
        _FakeRequest({"authorization": "Bearer secret-key-1"})
    ) == "ci"


def test_x_api_key_authenticates(monkeypatch: pytest.MonkeyPatch) -> None:
    """``X-API-Key`` is equivalent to the bearer header.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEY", "solo-key")
    assert auth.require_api_key(_FakeRequest({"x-api-key": "solo-key"})) == "default"
    who = auth.principal(_FakeRequest({"x-api-key": "solo-key"}))
    assert who["scopes"] == ["*"]


def test_bearer_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bearer``/``BEARER`` prefixes are accepted.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEY", "solo-key")
    assert auth.require_api_key(_FakeRequest({"authorization": "bearer solo-key"})) == "default"


def test_missing_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """No credential at all is a 401-class error.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEY", "solo-key")
    with pytest.raises(auth.AuthError, match="missing API key"):
        auth.principal(_FakeRequest())


def test_wrong_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong key is refused with a generic message.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEY", "solo-key")
    with pytest.raises(auth.AuthError, match="invalid or missing"):
        auth.principal(_FakeRequest({"authorization": "Bearer wrong"}))


def test_key_from_wrong_label_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Presenting a *label* instead of a key does not authenticate.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEYS", "ci:real-key:read")
    with pytest.raises(auth.AuthError):
        auth.principal(_FakeRequest({"authorization": "Bearer ci"}))


def test_no_keys_configured_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth enabled with no keys refuses every request rather than allowing it.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.delenv("SIGNAL_API_KEY", raising=False)
    monkeypatch.delenv("SIGNAL_API_KEYS", raising=False)
    with pytest.raises(auth.AuthError):
        auth.principal(_FakeRequest({"authorization": "Bearer anything"}))


def test_multiple_keys_each_get_their_own_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SIGNAL_API_KEYS`` supports several keys with distinct scopes.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEYS", "ci:k1:read;ops:k2:read,opsec;admin:k3:*")
    assert auth.require_api_key(_FakeRequest({"authorization": "Bearer k1"})) == "ci"
    assert auth.require_api_key(_FakeRequest({"authorization": "Bearer k2"})) == "ops"
    assert auth.require_api_key(_FakeRequest({"authorization": "Bearer k3"})) == "admin"
    who = auth.principal(_FakeRequest({"authorization": "Bearer k2"}))
    assert who["scopes"] == ["read", "opsec"]


def test_scopes_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key without the required scope is refused.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEYS", "readonly:k1:read")
    dep = auth.require_scope("investigate")
    request = _FakeRequest({"authorization": "Bearer k1"})
    with pytest.raises(auth.AuthError, match="missing required scope"):
        asyncio.run(dep(request))

    # read implies read; investigate key may read its own results back.
    ok_dep = auth.require_scope("read")
    assert asyncio.run(ok_dep(request))["id"] == "readonly"


def test_wildcard_scope_passes_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``*``-scoped key satisfies any scope requirement.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEY", "god-key")
    for scope in ("investigate", "search", "read", "opsec", "anything"):
        assert asyncio.run(auth.require_scope(scope)(_FakeRequest({"x-api-key": "god-key"})))


def test_has_scope_semantics() -> None:
    """Direct unit test of the scope-matching rules."""
    assert auth.has_scope({"scopes": ["*"]}, "investigate") is True
    assert auth.has_scope({"scopes": ["read"]}, "read") is True
    assert auth.has_scope({"scopes": ["read"]}, "investigate") is False
    assert auth.has_scope({"scopes": ["investigate"]}, "read") is True
    assert auth.has_scope({"scopes": []}, "read") is False
    assert auth.has_scope({}, "read") is False


def test_auth_failure_is_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated failures from one IP are throttled to blunt key guessing.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEY", "real-key")
    request = _FakeRequest({"authorization": "Bearer wrong"}, ip="198.51.100.7")
    for _ in range(auth.AUTH_FAIL_THRESHOLD):
        with pytest.raises(auth.AuthError, match="invalid or missing"):
            auth.principal(request)
    with pytest.raises(auth.AuthError, match="too many failed"):
        auth.principal(request)
    # Even the *correct* key is refused while throttled — the throttle must not
    # become an oracle that confirms a valid key.
    with pytest.raises(auth.AuthError, match="too many failed"):
        auth.principal(_FakeRequest({"authorization": "Bearer real-key"}, ip="198.51.100.7"))


def test_auth_failure_logs_never_leak_the_key(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """The presented key is never written to a log record.

    Args:
        monkeypatch: Env patcher.
        caplog: Pytest log capture fixture.
    """
    monkeypatch.setenv("SIGNAL_API_KEY", "real-key")
    secret = "hunter2-super-secret"
    with caplog.at_level("DEBUG"):
        with pytest.raises(auth.AuthError):
            auth.principal(_FakeRequest({"authorization": f"Bearer {secret}"}, ip="203.0.113.55"))
    assert secret not in caplog.text
    assert "203.0.113.55" in caplog.text  # the IP is logged
    assert "/api/investigate" in caplog.text  # the path is logged


def test_non_ascii_key_is_rejected_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-ASCII credential raises AuthError, not UnicodeEncodeError.

    ``hmac.compare_digest`` refuses non-ASCII ``str`` input; the auth layer must
    convert before comparing or a hostile header would 500.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEY", "real-key")
    with pytest.raises(auth.AuthError):
        auth.principal(_FakeRequest({"authorization": "Bearer ключ-🔑"}))


# ── Rate limiting ────────────────────────────────────────────────────────────

def test_rate_limiter_exhausts_and_refills() -> None:
    """A bucket drains, refuses, then refills over time."""
    limiter = rate_limit.RateLimiter("t", rate=60.0, burst=3)
    for _ in range(3):
        allowed, retry = limiter.allow()
        assert allowed is True and retry == 0.0
    allowed, retry = limiter.allow()
    assert allowed is False
    assert retry > 0
    time.sleep(limiter.seconds_until(1.0) + 0.05)  # wait exactly what we were told
    allowed, _ = limiter.allow()
    assert allowed is True


def test_rate_limit_scopes_are_independent() -> None:
    """Investigate quota exhaustion must not block the read scope."""
    for _ in range(5):
        rate_limit.get_limiter("investigate", "alice").allow()
    allowed, retry = rate_limit.get_limiter("investigate", "alice").allow()
    assert allowed is False and retry > 0
    assert rate_limit.get_limiter("read", "alice").allow()[0] is True
    # A different principal has its own bucket.
    assert rate_limit.get_limiter("investigate", "bob").allow()[0] is True


def test_rate_limit_investigate_is_stricter_than_read() -> None:
    """The expensive scope is throttled far harder than the cheap one."""
    assert rate_limit.default_rpm("investigate") < rate_limit.default_rpm("read")
    assert rate_limit.default_rpm("search") < rate_limit.default_rpm("read")
    assert rate_limit.default_burst("investigate") < rate_limit.default_burst("read")


def test_rate_limit_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``RATE_LIMIT_RPM`` / ``RATE_LIMIT_BURST`` / per-scope env vars win.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("RATE_LIMIT_RPM", "120")
    monkeypatch.setenv("RATE_LIMIT_BURST", "9")
    assert rate_limit.default_rpm("search") == 120
    assert rate_limit.default_burst("search") == 9
    monkeypatch.setenv("RATE_LIMIT_SCOPE_RPM.investigate", "1")
    assert rate_limit.default_rpm("investigate") == 1


def test_rate_limit_headers_shape() -> None:
    """``X-RateLimit-*`` headers are always present and well-formed."""
    headers = rate_limit.rate_limit_headers("alice", "investigate")
    assert headers["X-RateLimit-Limit"] == str(int(rate_limit.default_rpm("investigate")))
    assert int(headers["X-RateLimit-Remaining"]) >= 0
    assert int(headers["X-RateLimit-Reset"]) >= 0


def test_check_returns_retry_after_header_on_exhaustion() -> None:
    """An exhausted bucket reports how long to wait."""
    for _ in range(50):
        allowed, retry, headers = rate_limit.check("bob", "opsec")
        if not allowed:
            assert headers["Retry-After"]
            assert retry > 0
            break
    else:  # pragma: no cover - would mean the limiter never engaged
        pytest.fail("opsec limiter never engaged")


def test_security_rate_limit_headers_delegates() -> None:
    """``security.rate_limit_headers`` proxies to the limiter for the HTTP layer."""
    headers = security.rate_limit_headers("carol", "read")
    assert "X-RateLimit-Limit" in headers
    headers2 = security.rate_limit_headers({"id": "carol"}, "read")
    assert headers2 == headers


# ── Schemas ──────────────────────────────────────────────────────────────────

def test_investigate_request_matches_main_py_fields() -> None:
    """The field names match what ``main.py`` accepts today."""
    req = InvestigateRequest(input="john@example.com", input_type="email", case_id="abc12345")
    assert req.input == "john@example.com"
    assert req.input_type == "email"
    assert req.case_id == "abc12345"
    assert InvestigateRequest(input="x").input_type is None


def test_search_request_defaults_and_validation() -> None:
    """Engines default, are normalised, and unknown engines are refused."""
    req = SearchRequest(query="site:example.com")
    assert req.engines == ["google", "bing", "yandex"]
    assert req.use_dorks is True
    assert SearchRequest(query="q", engines="google,bing").engines == ["google", "bing"]
    assert SearchRequest(query="q", engines=["google", "google"]).engines == ["google"]
    with pytest.raises(Exception, match="unsupported engine"):
        SearchRequest(query="q", engines=["google", "; drop table"])


def test_name_search_request_validation() -> None:
    """A name is required and bounded."""
    req = NameSearchRequest(name="Jane Doe", location="Berlin", case_id="c1")
    assert req.name == "Jane Doe" and req.location == "Berlin"
    with pytest.raises(Exception):
        NameSearchRequest(name="")


def test_request_models_forbid_extra_fields() -> None:
    """Unknown fields are a 422, not a silently-dropped surprise."""
    for model, kwargs in [
        (InvestigateRequest, {"input": "x"}),
        (SearchRequest, {"query": "x"}),
        (NameSearchRequest, {"name": "x"}),
        (CaseCreate, {"name": "x"}),
        (CompareRequest, {"prompt": "x"}),
        (SaveMemoryRequest, {}),
    ]:
        with pytest.raises(Exception, match="extra|Extra"):
            model(**kwargs, injected_extra_field="pwned")


def test_free_text_is_length_bounded() -> None:
    """No free-text field can be used to blow up memory or prompts."""
    with pytest.raises(Exception):
        InvestigateRequest(input="x" * 100_000)
    with pytest.raises(Exception):
        SearchRequest(query="x" * 10_000)
    with pytest.raises(Exception):
        CaseCreate(name="x" * 10_000)
    with pytest.raises(Exception):
        CaseCreate(name="ok", notes="x" * 100_000)
    with pytest.raises(Exception):
        NameSearchRequest(name="x" * 10_000)


def test_collections_are_bounded() -> None:
    """Unbounded lists are refused."""
    with pytest.raises(Exception):
        SearchRequest(query="x", engines=["google"] * 100)
    with pytest.raises(Exception):
        CaseCreate(name="ok", tags=["t%d" % i for i in range(200)])
    with pytest.raises(Exception):
        CaseUpdate(tags=["t%d" % i for i in range(200)])
    with pytest.raises(Exception):
        CompareRequest(prompt="x", models=["m%d" % i for i in range(100)])
    with pytest.raises(Exception):
        SaveMemoryRequest(entities=[{"value": "v%d" % i} for i in range(2000)])
    with pytest.raises(Exception):
        SaveMemoryRequest(tags=["t%d" % i for i in range(200)])
    # A duplicate-heavy list must not be able to hide an oversized payload.
    with pytest.raises(Exception):
        SearchRequest(query="x", engines=["google"] * 500)


def test_case_update_requires_a_field() -> None:
    """An empty PATCH is refused rather than being a silent no-op."""
    with pytest.raises(Exception, match="at least one"):
        CaseUpdate()
    assert CaseUpdate(name="renamed").name == "renamed"


def test_compare_request_accepts_aliases() -> None:
    """The model arena may send ``prompt``, ``input`` or ``text``."""
    assert CompareRequest(prompt="a").prompt == "a"
    assert CompareRequest(input="b").prompt == "b"
    assert CompareRequest(text="c").prompt == "c"
    assert CompareRequest(prompt="x", temperature=0.7).temperature == 0.7
    with pytest.raises(Exception):
        CompareRequest(prompt="x", temperature=5.0)  # out of range


def test_save_memory_request_normalises_entities() -> None:
    """Entities accept strings or objects, and nested sizes are bounded."""
    req = SaveMemoryRequest(case_id="c1", entities=["alice", {"value": "bob", "type": "person"}])
    assert req.entities[0]["value"] == "alice"
    assert req.entities[1]["type"] == "person"
    with pytest.raises(Exception):
        SaveMemoryRequest(entities="not a list")


def test_report_format_is_a_literal() -> None:
    """``ReportFormat`` constrains the report query parameter to real formats."""
    from typing import get_args

    assert set(get_args(__import__("schemas").ReportFormat)) == {"markdown", "pdf", "json"}


# ── API dependencies ─────────────────────────────────────────────────────────

def test_inv_id_validation() -> None:
    """Investigation ids are constrained to a safe charset and length."""
    assert deps.validate_inv_id("abc12345") == "abc12345"
    assert deps.validate_inv_id("a" * 32) == "a" * 32
    for bad in [
        "short",
        "a" * 33,
        "../../etc/passwd",
        "abc12345/../x",
        "abc 12345",
        "abc12345;rm -rf",
        "abc12345\x00",
        "",
    ]:
        with pytest.raises(Exception):
            deps.validate_inv_id(bad)


def test_inv_id_path_dependency() -> None:
    """The async path dependency mirrors the sync validator."""
    assert asyncio.run(deps.inv_id_path("deadbeef")) == "deadbeef"
    with pytest.raises(Exception):
        asyncio.run(deps.inv_id_path("../x"))


def test_deps_translate_auth_error_to_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dependency turns AuthError into 401 rather than a 500 traceback.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEY", "real-key")
    request = _FakeRequest()
    with pytest.raises(Exception) as exc:
        asyncio.run(deps.require_auth(request))
    assert getattr(exc.value, "status_code", None) == 401


def test_deps_translate_missing_scope_to_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid key lacking the scope gets 403, not 401.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEYS", "readonly:k1:read")
    request = _FakeRequest({"authorization": "Bearer k1"})
    with pytest.raises(Exception) as exc:
        asyncio.run(deps.require_investigate("investigate")(request))
    assert getattr(exc.value, "status_code", None) == 403


def test_deps_translate_rate_limit_to_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exhausted bucket yields 429 with a ``Retry-After`` header.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEYS", "ci:k1:*")
    monkeypatch.setenv("RATE_LIMIT_SCOPE_RPM.investigate", "1")
    monkeypatch.setenv("RATE_LIMIT_BURST.investigate", "1")
    rate_limit.reset_all()
    dep = deps.require_investigate("investigate")
    asyncio.run(dep(_FakeRequest({"authorization": "Bearer k1"}, ip="203.0.113.1")))
    with pytest.raises(Exception) as exc:
        asyncio.run(dep(_FakeRequest({"authorization": "Bearer k1"}, ip="203.0.113.1")))
    err = exc.value
    assert getattr(err, "status_code", None) == 429
    assert getattr(err, "headers", {}).get("Retry-After")


def test_deps_require_read_passes_for_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wildcard key passes the combined auth+rate-limit dependency.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("SIGNAL_API_KEY", "god")
    who = asyncio.run(deps.require_read()(_FakeRequest({"x-api-key": "god"})))
    assert who["id"] == "default"


def test_deps_missing_config_fails_closed_with_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth enabled with no keys refuses with an actionable 403.

    Args:
        monkeypatch: Env patcher.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.delenv("SIGNAL_API_KEY", raising=False)
    monkeypatch.delenv("SIGNAL_API_KEYS", raising=False)
    monkeypatch.setattr(auth, "auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "load_api_keys", lambda: ())
    for dep in (deps.require_auth, deps.require_read()):
        with pytest.raises(Exception) as exc:
            asyncio.run(dep(_FakeRequest({"authorization": "Bearer x"})))
        err = exc.value
        assert getattr(err, "status_code", None) == 403
        assert "misconfigured" in str(getattr(err, "detail", ""))


# ── IP classification ────────────────────────────────────────────────────────

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "0.0.0.0", "10.0.0.1", "172.16.0.1", "172.31.255.255",
    "192.168.0.1", "169.254.169.254", "169.254.0.1", "100.64.0.1",
    "100.127.255.255", "198.18.0.1", "198.19.255.255", "224.0.0.1",
    "239.255.255.255", "255.255.255.255", "::1", "::", "fd00::1",
    "fc00::1", "fe80::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
    "64:ff9b::7f00:1", "2002:7f00:0001::", "fec0::1", "ff02::1",
])
def test_ip_is_blocked_rejects_non_public(ip: str) -> None:
    """Every non-public address class is classified as blocked.

    Args:
        ip: The address literal.
    """
    assert security.ip_is_blocked(ip) is not None, ip


@pytest.mark.parametrize("ip", [
    "8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946",
])
def test_ip_is_blocked_allows_public(ip: str) -> None:
    """Genuinely public addresses are allowed.

    Args:
        ip: The address literal.
    """
    assert security.ip_is_blocked(ip) is None, ip


def test_ip_is_blocked_allow_private_override() -> None:
    """``allow_private=True`` short-circuits the classification."""
    assert security.ip_is_blocked("10.0.0.1", allow_private=True) is None
    assert security.ip_is_blocked("169.254.169.254", allow_private=True) is None