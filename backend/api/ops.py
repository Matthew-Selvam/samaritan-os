"""ops.py — operational surface + the shared compatibility layer for `api/`.

Owned by WS-API. `main.py` mounts exactly two objects from this module:

    from api.ops import router, ws_router
    app.include_router(router)        # GET /api/health, /api/health/deep,
                                     # /api/metrics, /api/config, /api/opsec/*
    app.include_router(ws_router)     # WS  /ws/pipeline/{inv_id}

Why the WebSocket lives on a *second* router: every other router in this
package declares its own prefix (`/api`, `/api/cases`) so `main.py` can mount
them with a bare `include_router(...)`, but the pipeline socket must stay at
the legacy root path `/ws/pipeline/{inv_id}` (CONTRACTS.md §10 backwards
compatibility list). `router` therefore carries `prefix="/api"` and `ws_router`
carries none.

This module is also where the shared plumbing lives, because it is the only
router with no request-body semantics: `investigate.py`, `cases.py`,
`agents.py` and `reports.py` import the helpers below instead of duplicating
them (WS-API owns exactly these five files, so the plumbing has to live in one
of them).

Shared helpers exported here:
  * ``resolve_store()``   — started ``store.get_store()`` singleton
  * ``auth_gate()``       — FastAPI dependency; delegates to WS-SEC when present
  * ``rate_limit_dep()``  — FastAPI dependency; sets ``X-RateLimit-*`` headers
  * ``paginate()``        — ``limit``/``offset`` window helper
  * ``Wshub`` / ``broadcast`` — pipeline event fan-out
  * ``render_pdf_bytes()``— dependency-free single/multi-page PDF writer
  * ``install_cors()``    — allowlist-only CORS (never ``*``)
  * ``MAX_UPLOAD_BYTES``, ``sanitize_filename``, ``ALLOWED_IMAGE_TYPES``

Every WS-SEC module (``security``, ``auth``, ``schemas``, ``deps``,
``rate_limit``) is imported lazily/defensively: WS-API may not depend on them
existing, and this package must import cleanly before WS-SEC lands.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import os
import time
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)

from observability import get_logger, get_metrics, redact

log = get_logger("signal-os.api.ops")

APP_NAME = "signal-os"
APP_VERSION = "0.1.0"

#: Process start, used for ``uptime_s`` on the health/metrics endpoints.
STARTED_AT = time.time()

# ── Page caps (every list endpoint is paginated) ──────────────────────────────
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: Seconds a deep-health probe may take before it is reported as down.
PROBE_TIMEOUT_S = 2.0

#: Server-side WebSocket keepalive interval (the browser client pings at 25s).
WS_HEARTBEAT_S = 20.0


# ════════════════════════════════════════════════════════════════════════════
# Defensive WS-SEC access
# ════════════════════════════════════════════════════════════════════════════
#
# WS-SEC owns security.py / auth.py / schemas.py / rate_limit.py / api/deps.py.
# They may not exist yet while WS-API builds, and they may change shape, so
# every one of them is resolved at call time through `_maybe_import` and every
# call is guarded. Nothing here raises because a sibling module is missing.

_MAX_UPLOAD_FALLBACK = 50 * 1024 * 1024  # matches CONTRACTS.md §8


def _maybe_import(module_name: str) -> Optional[Any]:
    """Import ``module_name`` if it exists, returning ``None`` otherwise.

    Args:
        module_name: Dotted module path, e.g. ``"security"``.

    Returns:
        The imported module, or ``None`` when it is absent or fails to import
        (a sibling workstream may still be mid-write).
    """
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 — optional dependency by design
        log.debug("optional module %s unavailable: %s", module_name, type(exc).__name__)
        return None


def max_upload_bytes() -> int:
    """Resolve the upload cap, preferring ``security.MAX_UPLOAD_BYTES``.

    Returns:
        Maximum accepted upload size in bytes. Falls back to ``env`` then to
        the CONTRACTS.md default of 50 MiB.
    """
    security = _maybe_import("security")
    value = getattr(security, "MAX_UPLOAD_BYTES", None) if security else None
    if not isinstance(value, (int, float)) or value <= 0:
        try:
            value = int(os.getenv("MAX_UPLOAD_BYTES", "") or _MAX_UPLOAD_FALLBACK)
        except ValueError:
            value = _MAX_UPLOAD_FALLBACK
    return int(value)


#: Snapshot used at import time for documentation/reporting; the live value is
#: :func:`max_upload_bytes`.
MAX_UPLOAD_BYTES = max_upload_bytes()

#: Content types accepted by the photo/video/audio upload endpoints.
ALLOWED_IMAGE_TYPES: frozenset[str] = frozenset({
    "image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp",
    "image/tiff", "image/avif", "image/heic", "image/heif",
})


def sanitize_filename(name: str, *, fallback: str = "upload") -> str:
    """Reduce an untrusted upload filename to a safe basename.

    Delegates to ``security.sanitize_filename`` when WS-SEC has landed and
    applies a local equivalent otherwise: strips every directory component,
    rejects traversal and control characters, and caps the length so a
    pathological name can never produce a pathological path.

    Args:
        name: Client-supplied filename (may be ``None``/empty/hazardous).
        fallback: Name used when nothing usable survives sanitisation.

    Returns:
        A filename that is safe to join onto a temp directory.
    """
    security = _maybe_import("security")
    fn = getattr(security, "sanitize_filename", None) if security else None
    if callable(fn):
        try:
            cleaned = fn(name or "")
            if cleaned:
                return cleaned
        except Exception:  # noqa: BLE001 — fall through to the local rule
            pass

    raw = os.path.basename(str(name or "").replace("\\", "/")).strip()
    cleaned = "".join(
        ch for ch in raw
        if ch.isalnum() or ch in "._- " or ord(ch) > 127
    ).strip(" .")
    cleaned = cleaned.lstrip(".") or ""
    return cleaned[:120] or fallback


# ── Store access ─────────────────────────────────────────────────────────────

async def resolve_store() -> Any:
    """Return the shared, started investigation store.

    A thin async wrapper over :func:`runtime.get_store_handle`, which is the
    canonical accessor (CONTRACTS.md §4: ``store = await get_store()`` then
    ``await store.start()``). Delegating rather than reimplementing matters:
    ``main.py``'s lifespan, the legacy inline endpoints and
    ``runtime.run_pipeline`` all resolve storage through that handle, so a
    second accessor would hand this router a *different* store instance than
    the rest of the app — the exact multi-worker split the runtime module was
    written to eliminate.

    Returns:
        A started ``store.Store`` implementation, degraded to in-memory rather
        than raising when the configured database is unreachable.
    """
    try:
        from runtime import get_store_handle

        return await get_store_handle()
    except Exception:  # noqa: BLE001 — runtime may be mid-edit; use store.py directly
        import store as store_mod

        return await store_mod.get_store()


# ── Auth (delegates to WS-SEC when present) ───────────────────────────────────

async def auth_gate(request: Request) -> Optional[dict]:
    """FastAPI dependency enforcing API-key auth when WS-SEC provides it.

    Resolved at *call* time so this package keeps importing (and keeps working
    open in development) before ``auth.py``/``deps.py`` exist. When WS-SEC is
    present the failure path is preserved exactly: an ``AuthError`` becomes a
    401 with a structured body, and the resolved principal is attached to
    ``request.state.principal``.

    Args:
        request: The incoming request.

    Returns:
        The principal dict, or ``None`` when auth is disabled/unconfigured.

    Raises:
        HTTPException: 401 when a key is required and missing or invalid.
    """
    auth = _maybe_import("auth")
    if auth is None:
        request.state.principal = {"id": "anonymous", "scopes": ["*"]}
        return request.state.principal

    try:
        key_id = auth.require_api_key(request)
    except Exception as exc:  # noqa: BLE001 — AuthError and anything a dep raises
        _log_auth_failure(request, exc)
        status = 401
        try:
            if int(getattr(exc, "status_code", 401)) == 403:
                status = 403
        except (TypeError, ValueError):
            status = 401
        raise HTTPException(
            status_code=status,
            detail={"error": "unauthorized", "reason": str(exc) or "invalid credentials"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    principal: dict = {"id": str(key_id), "scopes": ["*"]}
    fn = getattr(auth, "principal", None)
    if callable(fn):
        try:
            resolved = fn(request)
            if isinstance(resolved, dict):
                principal = resolved
        except Exception:  # noqa: BLE001 — keep the minimal principal
            pass
    request.state.principal = principal
    return principal


def _log_auth_failure(request: Request, exc: BaseException) -> None:
    """Record an auth rejection with the caller's IP and path, never the key.

    Args:
        request: The rejected request.
        exc: The exception WS-SEC raised.
    """
    ip = request.client.host if request.client else "unknown"
    log.warning("auth rejected ip=%s path=%s reason=%s", ip, request.url.path, type(exc).__name__)


def current_principal(request: Request) -> dict:
    """Return the principal attached by :func:`auth_gate`.

    Args:
        request: The current request.

    Returns:
        ``{"id", "scopes", "label"}``; anonymous when auth never ran.
    """
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, dict):
        return principal
    return {"id": "anonymous", "scopes": ["*"], "label": "anonymous"}


def has_scope(request: Request, scope: str) -> bool:
    """Report whether the current principal holds ``scope``.

    Args:
        request: The current request.
        scope: Scope name, e.g. ``"write"``.

    Returns:
        ``True`` when the principal holds the scope or the wildcard.
    """
    scopes = current_principal(request).get("scopes") or []
    return "*" in scopes or scope in scopes


# ── Rate limiting ────────────────────────────────────────────────────────────

_BUCKETS: dict[str, dict[str, float]] = {}
_WINDOW_S = 60.0


def _rate_settings() -> tuple[int, int]:
    """Read the per-minute request budget from the environment.

    Returns:
        ``(rpm, burst)`` with sane defaults (60 rpm, 20 burst).
    """

    def _int(name: str, default: int) -> int:
        try:
            return max(1, int(os.getenv(name, "") or default))
        except ValueError:
            return default

    return _int("RATE_LIMIT_RPM", 60), _int("RATE_LIMIT_BURST", 20)


def _bucket(scope: str, identity: str, rpm: int, burst: int) -> dict[str, float]:
    """Consume one token from an in-process fixed-window bucket.

    The bucket backs the ``X-RateLimit-*`` response headers. Enforcement is
    delegated to WS-SEC's ``rate_limit.get_limiter`` when it exists; this keeps
    the headers meaningful either way.

    Args:
        scope: Route/limiter scope.
        identity: Caller identity (API key id, else client IP).
        rpm: Requests per window.
        burst: Maximum requests inside one window.

    Returns:
        ``{"allowed", "remaining", "reset", "retry_after", "limit", "burst"}``.
    """
    now = time.time()
    key = f"{scope}|{identity}"
    state = _BUCKETS.get(key)
    if state is None or now - state["start"] >= _WINDOW_S:
        state = {"start": now, "count": 0.0, "allowed": 1.0}
        _BUCKETS[key] = state
    if state["count"] >= burst:
        return {
            "allowed": False,
            "remaining": 0,
            "reset": int(state["start"] + _WINDOW_S),
            "retry_after": max(1.0, state["start"] + _WINDOW_S - now),
            "limit": rpm,
            "burst": burst,
        }
    state["count"] += 1
    state["allowed"] = float(state["count"])
    return {
        "allowed": True,
        "remaining": max(0, burst - int(state["count"])),
        "reset": int(state["start"] + _WINDOW_S),
        "retry_after": 0.0,
        "limit": rpm,
        "burst": burst,
    }


def rate_limit_check(request: Request) -> dict[str, Any]:
    """Decide whether a request is within its rate limit and build the headers.

    Args:
        request: The incoming request.

    Returns:
        ``{"allowed", "headers", "limit", "remaining"}``. ``headers`` is safe to
        spread onto any response.
    """
    rpm, burst = _rate_settings()
    scope = request.url.path
    identity = str(current_principal(request).get("id") or "anonymous")
    if identity == "anonymous" and request.client:
        identity = f"anonymous:{request.client.host}"

    decision = _bucket(scope, identity, rpm, burst)
    allowed = bool(decision["allowed"])

    limiter = None
    rate_limit = _maybe_import("rate_limit")
    getter = getattr(rate_limit, "get_limiter", None) if rate_limit else None
    if callable(getter):
        try:
            limiter = getter(scope)
            verdict = limiter.allow()
            if isinstance(verdict, tuple) and len(verdict) == 2:
                allowed = bool(verdict[0])
                decision["retry_after"] = float(verdict[1] or 0.0)
            else:
                allowed = bool(verdict)
        except Exception as exc:  # noqa: BLE001 — never fail a request on limiting
            log.debug("rate limiter unavailable for %s: %s", scope, type(exc).__name__)

    headers = {
        "X-RateLimit-Limit": str(decision["limit"]),
        "X-RateLimit-Burst": str(decision["burst"]),
        "X-RateLimit-Remaining": str(decision["remaining"]),
        "X-RateLimit-Reset": str(decision["reset"]),
    }
    if not allowed:
        retry_after = max(1, int(decision.get("retry_after") or 0) or 1)
        headers["Retry-After"] = str(retry_after)
    get_metrics().incr("api.rate_limited", tags={"allowed": str(allowed).lower()})
    return {"allowed": allowed, "headers": headers, "decision": decision}


async def rate_limit_dep(request: Request, response: Response) -> dict[str, Any]:
    """FastAPI dependency applying the rate limit and stamping the headers.

    Args:
        request: The incoming request.
        response: The outgoing response, used to attach ``X-RateLimit-*``.

    Returns:
        The :func:`rate_limit_check` result.

    Raises:
        HTTPException: 429 with ``Retry-After`` when the budget is exhausted.
    """
    result = rate_limit_check(request)
    for key, value in result["headers"].items():
        response.headers[key] = value
    if not result["allowed"]:
        get_metrics().incr("api.rate_limit.rejected")
        raise HTTPException(
            status_code=429,
            detail={"error": "rate limit exceeded", "retry_after": result["headers"].get("Retry-After")},
            headers=result["headers"],
        )
    return result


# ── Pagination ───────────────────────────────────────────────────────────────

def clamp_limit(limit: Optional[int], *, default: int = DEFAULT_LIMIT,
                maximum: int = MAX_LIMIT) -> int:
    """Clamp a client-supplied page size into a sane range.

    Args:
        limit: Requested page size (may be ``None``/negative/huge).
        default: Page size when none was supplied.
        maximum: Hard ceiling.

    Returns:
        An integer in ``[1, maximum]``.
    """
    try:
        value = int(limit) if limit is not None else int(default)
    except (TypeError, ValueError):
        value = int(default)
    return max(1, min(int(value), int(maximum)))


def clamp_offset(offset: Optional[int]) -> int:
    """Clamp a client-supplied offset to a non-negative integer.

    Args:
        offset: Requested offset.

    Returns:
        ``max(0, offset)``.
    """
    try:
        return max(0, int(offset or 0))
    except (TypeError, ValueError):
        return 0


def paginate(items: list, limit: Optional[int], offset: Optional[int]) -> list:
    """Slice an already-materialised list into one page.

    Args:
        items: The full list, newest-first as produced by the store.
        limit: Requested page size.
        offset: Requested offset.

    Returns:
        The requested window (possibly empty).
    """
    start = clamp_offset(offset)
    if start >= len(items):
        return []
    return items[start:start + clamp_limit(limit)]


def paged(items: list, limit: Optional[int], offset: Optional[int], **extra: Any) -> dict:
    """Build the uniform ``{items, total, limit, offset}`` envelope.

    Args:
        items: The full list.
        limit: Requested page size.
        offset: Requested offset.
        **extra: Additional top-level keys merged into the envelope.

    Returns:
        A JSON-safe pagination envelope.
    """
    start = clamp_offset(offset)
    size = clamp_limit(limit)
    return {
        "items": paginate(items, size, start),
        "total": len(items),
        "limit": size,
        "offset": start,
        **extra,
    }


# ════════════════════════════════════════════════════════════════════════════
# Pipeline WebSocket hub
# ════════════════════════════════════════════════════════════════════════════


def _now_iso() -> str:
    """Return the current UTC instant as an ISO-8601 string with a ``Z`` suffix.

    Returns:
        An ISO-8601 timestamp.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Wshub:
    """Fan-out of pipeline events to subscribed WebSocket clients.

    One entry per investigation id. Dead sockets are pruned on every broadcast
    so a client that vanishes mid-run cannot leak an entry.

    **There is exactly one bus in the process.** ``runtime.py`` owns the
    canonical registry (``register_ws``/``unregister_ws``/``broadcast``), and
    that is what ``runtime.run_pipeline`` publishes to. This hub therefore
    *delegates* to the runtime registry rather than keeping a private one:
    two parallel registries would mean a socket subscribed through this
    endpoint silently never receives the step/done events the pipeline emits,
    which is the kind of bug that only shows up as a dashboard that never
    finishes. The local dict is a fallback for when ``runtime`` is unavailable.
    """

    def __init__(self) -> None:
        """Create an empty hub."""
        self._clients: dict[str, list[WebSocket]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _runtime_registry() -> Optional[tuple[Any, Any, Any]]:
        """Resolve the canonical runtime bus, when it exists.

        Returns:
            ``(register, unregister, broadcast)`` callables, or ``None``.
        """
        runtime = _maybe_import("runtime")
        if runtime is None:
            return None
        register = getattr(runtime, "register_ws", None)
        unregister = getattr(runtime, "unregister_ws", None)
        publish = getattr(runtime, "broadcast", None)
        if callable(register) and callable(unregister) and callable(publish):
            return (register, unregister, publish)
        return None

    async def subscribe(self, inv_id: str, ws: WebSocket) -> None:
        """Register ``ws`` as a subscriber of ``inv_id``.

        Args:
            inv_id: Investigation channel.
            ws: The accepted socket.
        """
        registry = self._runtime_registry()
        if registry is not None:
            await registry[0](str(inv_id), ws)
            return
        async with self._lock:
            self._clients.setdefault(str(inv_id), []).append(ws)
        get_metrics().gauge("api.ws.clients", float(len(self._clients)))

    async def unsubscribe(self, inv_id: str, ws: WebSocket) -> None:
        """Remove ``ws`` from ``inv_id`` and drop empty channels.

        Args:
            inv_id: Investigation channel.
            ws: The socket being closed.
        """
        registry = self._runtime_registry()
        if registry is not None:
            await registry[1](str(inv_id), ws)
            return
        async with self._lock:
            subs = self._clients.get(str(inv_id))
            if subs and ws in subs:
                subs.remove(ws)
            if subs is not None and not subs:
                self._clients.pop(str(inv_id), None)
        get_metrics().gauge("api.ws.clients", float(len(self._clients)))

    def subscribers(self, inv_id: str) -> int:
        """Return how many sockets are subscribed to ``inv_id``.

        Args:
            inv_id: Investigation channel.

        Returns:
            The subscriber count.
        """
        runtime = _maybe_import("runtime")
        counter = getattr(runtime, "ws_client_count", None) if runtime else None
        if callable(counter):
            try:
                return int(counter(str(inv_id)))
            except Exception:  # noqa: BLE001
                pass
        return len(self._clients.get(str(inv_id), []))

    async def broadcast(self, inv_id: str, payload: dict) -> int:
        """Send one event to every subscriber of ``inv_id``.

        Args:
            inv_id: Investigation channel.
            payload: Event body; ``inv_id`` and ``ts`` are added when absent.

        Returns:
            The number of sockets the event reached.
        """
        event = {"inv_id": str(inv_id), "ts": _now_iso(), **payload}
        registry = self._runtime_registry()
        if registry is not None:
            await registry[2](str(inv_id), event)
            return self.subscribers(inv_id)
        return await self._broadcast_local(str(inv_id), event)

    async def _broadcast_local(self, inv_id: str, event: dict) -> int:
        """Fan out using the private registry (fallback path).

        Args:
            inv_id: Investigation channel.
            event: The already-stamped event body.

        Returns:
            The number of sockets the event reached.
        """
        subs = list(self._clients.get(inv_id, []))
        if not subs:
            return 0
        try:
            body = json.dumps(event, default=str)
        except (TypeError, ValueError):
            body = json.dumps({"type": "error", "inv_id": inv_id, "ts": _now_iso(),
                               "error": "event not serialisable"})
        dead: list[WebSocket] = []
        delivered = 0
        for ws in subs:
            try:
                await ws.send_text(body)
                delivered += 1
            except Exception:  # noqa: BLE001 — a broken socket must not stop the rest
                dead.append(ws)
        for ws in dead:
            await self.unsubscribe(inv_id, ws)
        return delivered

    async def close_all(self) -> None:
        """Close every subscription; called on application shutdown."""
        async with self._lock:
            sockets = [ws for subs in self._clients.values() for ws in subs]
            self._clients.clear()
        for ws in sockets:
            try:
                await ws.close(code=1001)
            except Exception:  # noqa: BLE001
                pass


#: Process-wide hub shared by ``investigate.py`` (producer) and the socket
#: endpoint below (consumer).
WS_HUB = Wshub()


async def broadcast(inv_id: str, payload: dict) -> int:
    """Broadcast a pipeline event on the shared hub.

    Args:
        inv_id: Investigation channel.
        payload: Event body (``type`` plus event-specific fields).

    Returns:
        Sockets reached.
    """
    return await WS_HUB.broadcast(inv_id, payload)


def ws_allowed_origins() -> tuple[list[str], bool]:
    """Resolve the WebSocket origin allowlist.

    Returns:
        ``(origins, strict)``. ``strict`` is ``True`` when ``WS_ALLOWED_ORIGINS``
        is configured, i.e. origins outside the list must be refused. When it
        is unset the endpoint stays open (local development default) and the
        caller is warned.
    """
    raw = (os.getenv("WS_ALLOWED_ORIGINS") or "").strip()
    if not raw:
        return ([], False)
    origins = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    return (origins, True)


# ════════════════════════════════════════════════════════════════════════════
# Dependency-free PDF rendering
# ════════════════════════════════════════════════════════════════════════════
#
# No PDF library is installed (and adding a mandatory one for a single export
# format would break the zero-infrastructure requirement), so reports are
# rendered with a minimal, standards-compliant PDF 1.4 writer: Helvetica text
# pages, Helvetica-1 WinAnsi encoding, correct xref offsets.

_PAGE_W, _PAGE_H = 612, 792          # US Letter, PostScript points
_MARGIN_X, _TOP_Y, _BOTTOM_Y = 54, 738, 60
_FONT_SIZE, _LEADING = 10, 13
_MAX_PAGES = 200


def _pdf_escape(text: str) -> bytes:
    """Escape a string for a PDF literal ``(...)`` string object.

    Args:
        text: Text to encode.

    Returns:
        Escaped bytes safe inside ``(...)``.
    """
    out = (text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)"))
    return out.encode("cp1252", errors="replace")


def _wrap(text: str, width: int) -> list[str]:
    """Greedy word-wrap one line of text to a character budget.

    The PDF writer uses a fixed-advance approximation of Helvetica; exact
    metrics are unnecessary for a readable export.

    Args:
        text: Source line.
        width: Maximum characters per output line.

    Returns:
        Wrapped lines (always at least one element).
    """
    words = str(text).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def render_pdf_bytes(title: str, markdown: str) -> bytes:
    """Render text into a minimal, valid PDF document.

    Args:
        title: Document title, drawn on the first page.
        markdown: Body text; markdown syntax is emitted verbatim (headings keep
            their ``#`` prefix) because the goal is a faithful, downloadable
            artefact rather than typographic perfection.

    Returns:
        A complete ``%PDF-1.4`` byte string with a correct cross-reference
        table. Returns an empty ``bytes`` only if the body is unencodable.
    """
    body: list[str] = [str(title or "Signal-OS report"), ""]
    body.extend(str(markdown or "").splitlines() or ["(no report body)"])
    # ~78 Helvetica chars per line at 10pt across the usable page width.
    wrapped: list[str] = []
    for line in body:
        wrapped.extend(_wrap(line, 78))

    chars_per_page = max(1, (_TOP_Y - _BOTTOM_Y) // _LEADING)
    pages = [
        wrapped[i:i + chars_per_page]
        for i in range(0, min(len(wrapped), chars_per_page * _MAX_PAGES), chars_per_page)
    ] or [[""]]

    objects: list[bytes] = []

    def _add(body_bytes: bytes) -> int:
        objects.append(body_bytes)
        return len(objects)

    font_id = _add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                   b"/Encoding /WinAnsiEncoding >>")
    content_ids: list[int] = []
    for lines in pages:
        content = ["BT", "/F1 %d Tf" % _FONT_SIZE, "%d TL" % _LEADING,
                   "1 0 0 1 %d %d Tm" % (_MARGIN_X, _TOP_Y)]
        for line in lines:
            content.append(f"({_pdf_escape(line[:200])}) Tj")
            content.append("T*")
        content.append("ET")
        stream = ("\n".join(content)).encode("latin-1", errors="replace")
        content_ids.append(_add(
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        ))

    pages_id = len(objects) + len(pages) + 1
    kids = " ".join(f"{cid} 0 R" for cid in content_ids)
    _add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids.encode(), len(pages)))
    page_ids = []
    for cid in content_ids:
        page_ids.append(_add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, _PAGE_W, _PAGE_H, font_id, cid)
        ))

    catalog_id = _add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    info_id = _add(b"<< /Title (%s) /Producer (Signal-OS) >>" % _pdf_escape(str(title)[:120]))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + payload + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, catalog_id, info_id, xref_at))
    return bytes(out)


# ── Zip export helper (in-memory, never touches the filesystem) ──────────────

def build_zip(entries: dict[str, str | bytes]) -> bytes:
    """Build a ZIP archive entirely in memory.

    Args:
        entries: ``{archive_path: content}``; ``str`` values are encoded UTF-8.

    Returns:
        The archive bytes, ready to stream as ``application/zip``.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            payload = content.encode("utf-8") if isinstance(content, str) else content
            archive.writestr(path, payload)
    return buffer.getvalue()


# ════════════════════════════════════════════════════════════════════════════
# CORS
# ════════════════════════════════════════════════════════════════════════════

#: Same-origin dev origins used when ``CORS_ORIGINS`` is unset.
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8766",
    "http://127.0.0.1:8766",
)


def cors_origins() -> list[str]:
    """Build the CORS allowlist from ``CORS_ORIGINS``.

    ``*`` is never returned: a wildcard origin combined with credentials is
    rejected by browsers anyway, and it silently disables the same-origin
    protection the frontend relies on. A configured ``*`` is dropped with a
    warning rather than honoured.

    Returns:
        A list of allowed origins (never empty unless explicitly configured).
    """
    raw = (os.getenv("CORS_ORIGINS") or "").strip()
    if not raw:
        return list(DEFAULT_CORS_ORIGINS)
    origins: list[str] = []
    for entry in raw.split(","):
        origin = entry.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            log.warning("CORS_ORIGINS contains '*' — ignored; list explicit origins instead")
            continue
        origins.append(origin)
    return origins or list(DEFAULT_CORS_ORIGINS)


def install_cors(app: Any) -> list[str]:
    """Install allowlist-only CORS middleware on ``app``.

    Exposed so ``main.py`` never has to hardcode ``allow_origins=["*"]``.

    Args:
        app: The FastAPI application.

    Returns:
        The origins that were allowed.
    """
    from fastapi.middleware.cors import CORSMiddleware

    origins = cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "Accept"],
        expose_headers=[
            "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
            "X-RateLimit-Burst", "Retry-After", "Content-Disposition",
        ],
        max_age=600,
    )
    log.info("CORS allowlist: %s", ", ".join(origins))
    return origins


# ════════════════════════════════════════════════════════════════════════════
# Health / config
# ════════════════════════════════════════════════════════════════════════════

router = APIRouter(prefix="/api", tags=["ops"])
ws_router = APIRouter(tags=["ops-ws"])


def _llm_health() -> dict:
    """Summarise the model tier without ever touching a key.

    Returns:
        ``{"available", "provider", "model", "error"}``.
    """
    try:
        from llm import get_llm

        llm = get_llm()
        health = llm.health()
        models = llm.available_models()
        primary = None
        providers = health.get("providers") or {}
        if isinstance(providers, dict):
            ready = [name for name, info in providers.items()
                     if isinstance(info, dict) and info.get("ready")]
            primary = ready[0] if ready else None
        return {
            "available": bool(health.get("ok") or models),
            "provider": primary,
            "model": models[0] if models else None,
            "error": None if (health.get("ok") or models) else "no provider configured",
        }
    except Exception as exc:  # noqa: BLE001 — health must never fail
        return {"available": False, "provider": None, "model": None,
                "error": f"{type(exc).__name__}"}


@router.get("/health")
async def health() -> dict:
    """Liveness plus a cheap dependency summary.

    Preserves the legacy body (``status``/``platform``/``version``) exactly and
    adds the fields the frontend ``HealthStatus`` type documents. Nothing here
    performs I/O that can block, so this endpoint always answers fast.

    Returns:
        ``{"status", "platform", "version", "uptime_s", "llm", "store",
        "cache", "agents"}``. ``status`` is ``"ok"``/``"degraded"``/``"down"``.
    """
    store_summary: dict[str, Any] = {"backend": "unknown", "degraded": False}
    try:
        store = await resolve_store()
        store_summary = {"backend": getattr(store, "backend", "unknown"),
                         "degraded": bool(getattr(store, "degraded", False))}
        health_fn = getattr(store, "health", None)
        if callable(health_fn):
            store_summary.update(health_fn())
    except Exception as exc:  # noqa: BLE001
        store_summary = {"backend": "unavailable", "degraded": True,
                         "error": f"{type(exc).__name__}"}

    cache_summary: dict[str, Any] = {}
    try:
        from cache import cache_backend, cache_stats

        stats = cache_stats()
        cache_summary = {
            "backend": cache_backend(),
            "hit_rate": stats.get("hit_rate"),
            "entries": stats.get("size"),
        }
    except Exception as exc:  # noqa: BLE001
        cache_summary = {"backend": "unavailable", "error": f"{type(exc).__name__}"}

    agent_summary: dict[str, int] = {"total": 0, "healthy": 0}
    try:
        from agents import AGENT_REGISTRY

        agent_summary = {"total": len(AGENT_REGISTRY), "healthy": len(AGENT_REGISTRY)}
    except Exception:  # noqa: BLE001
        pass

    llm_summary = _llm_health()
    status = "ok"
    if store_summary.get("degraded") or not llm_summary.get("available"):
        status = "degraded"
    if store_summary.get("backend") == "unavailable":
        status = "down"

    return {
        "status": status,
        "platform": APP_NAME,
        "version": APP_VERSION,
        "uptime_s": round(time.time() - STARTED_AT, 3),
        "llm": llm_summary,
        "store": store_summary,
        "cache": cache_summary,
        "agents": agent_summary,
    }


async def _probe_postgres() -> tuple[bool, str]:
    """Probe PostgreSQL with a short connect timeout.

    Returns:
        ``(ok, detail)``.
    """
    try:
        import config
        import psycopg2  # noqa: PLC0415 — optional driver, imported lazily
    except Exception as exc:  # noqa: BLE001
        return False, f"driver unavailable: {type(exc).__name__}"

    def _connect() -> None:
        conn = psycopg2.connect(config.DATABASE_URL, connect_timeout=int(PROBE_TIMEOUT_S))
        conn.close()

    try:
        await asyncio.wait_for(asyncio.to_thread(_connect), timeout=PROBE_TIMEOUT_S + 0.5)
        return True, "ok"
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}"


async def _probe_redis() -> tuple[bool, str]:
    """Probe Redis by issuing a PING.

    Returns:
        ``(ok, detail)``.
    """
    try:
        import config
        import redis.asyncio as aioredis  # noqa: PLC0415 — optional driver
    except Exception as exc:  # noqa: BLE001
        return False, f"driver unavailable: {type(exc).__name__}"
    client = None
    try:
        client = aioredis.from_url(config.REDIS_URL,
                                   socket_connect_timeout=PROBE_TIMEOUT_S)
        await asyncio.wait_for(client.ping(), timeout=PROBE_TIMEOUT_S + 0.5)
        return True, "ok"
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}"
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.aclose(), timeout=1.0)
            except Exception:  # noqa: BLE001
                pass


async def _probe_neo4j() -> tuple[bool, str]:
    """Probe Neo4j with ``verify_connectivity`` in a worker thread.

    Returns:
        ``(ok, detail)``.
    """
    try:
        import config
        from neo4j import GraphDatabase  # noqa: PLC0415 — optional driver
    except Exception as exc:  # noqa: BLE001
        return False, f"driver unavailable: {type(exc).__name__}"

    def _verify() -> None:
        auth = (config.NEO4J_USER, config.NEO4J_PASSWORD) if config.NEO4J_PASSWORD else None
        driver = GraphDatabase.driver(config.NEO4J_URI, auth=auth)
        try:
            driver.verify_connectivity()
        finally:
            driver.close()

    try:
        await asyncio.wait_for(asyncio.to_thread(_verify), timeout=PROBE_TIMEOUT_S + 1.0)
        return True, "ok"
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}"


async def _probe_http(name: str, url: str) -> tuple[bool, str]:
    """Probe an HTTP dependency's readiness endpoint.

    Args:
        name: Dependency label, used in the returned detail string.
        url: Absolute readiness URL.

    Returns:
        ``(ok, detail)``.
    """
    try:
        import httpx  # noqa: PLC0415 — optional but usually present
    except Exception as exc:  # noqa: BLE001
        return False, f"driver unavailable: {type(exc).__name__}"
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(url)
        if response.status_code >= 400:
            return False, f"http {response.status_code}"
        return True, "ok"
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}"


async def _probe_minio() -> tuple[bool, str]:
    """Probe MinIO's readiness endpoint.

    Returns:
        ``(ok, detail)``.
    """
    import config

    endpoint = str(config.MINIO_ENDPOINT or "").strip()
    if not endpoint:
        return False, "not configured"
    base = endpoint if "://" in endpoint else f"http://{endpoint}"
    return await _probe_http("minio", f"{base.rstrip('/')}/minio/health/ready")


@router.get("/health/deep")
async def health_deep() -> dict:
    """Deep health: probe every backing service concurrently.

    Every probe is individually time-boxed and failure-tolerant: this endpoint
    always returns HTTP 200 with a per-dependency verdict, because a dependency
    being down is *information*, not a failed request. Anything reported as
    down is optional for the platform to function — every agent degrades
    gracefully without them.

    Returns:
        ``{"status", "uptime_s", "checks", "store", "llm", "cache"}`` where
        ``checks`` maps dependency name to ``{"ok", "detail", "ms"}``.
    """
    started = time.monotonic()
    probes: dict[str, Callable[[], Any]] = {
        "postgres": _probe_postgres,
        "redis": _probe_redis,
        "neo4j": _probe_neo4j,
        "minio": _probe_minio,
    }
    try:
        import config

        probes["qdrant"] = lambda: _probe_http(
            "qdrant", f"{str(config.QDRANT_URL).rstrip('/')}/readyz")
    except Exception as exc:  # noqa: BLE001
        probes["qdrant"] = lambda: _no_config("config unavailable", exc)

    results = await asyncio.gather(
        *(fn() for fn in probes.values()), return_exceptions=True
    )
    checks: dict[str, dict[str, Any]] = {}
    for name, outcome in zip(probes, results):
        if isinstance(outcome, BaseException):
            checks[name] = {"ok": False, "detail": f"probe raised {type(outcome).__name__}"}
            continue
        ok, detail = outcome
        checks[name] = {"ok": bool(ok), "detail": detail}

    store_summary: dict[str, Any] = {}
    try:
        store = await resolve_store()
        store_summary = {"backend": getattr(store, "backend", "unknown"),
                         "degraded": bool(getattr(store, "degraded", False))}
        health_fn = getattr(store, "health", None)
        if callable(health_fn):
            store_summary.update(health_fn())
    except Exception as exc:  # noqa: BLE001
        store_summary = {"backend": "unavailable", "degraded": True,
                         "error": f"{type(exc).__name__}"}

    cache_summary: dict[str, Any] = {}
    try:
        from cache import cache_backend, cache_stats

        stats = cache_stats()
        cache_summary = {"backend": cache_backend(), "hit_rate": stats.get("hit_rate"),
                         "errors": stats.get("errors")}
    except Exception as exc:  # noqa: BLE001
        cache_summary = {"backend": "unavailable", "error": f"{type(exc).__name__}"}

    required_ok = not store_summary.get("degraded", False) and store_summary.get(
        "backend") != "unavailable"
    optional_up = sum(1 for c in checks.values() if c["ok"])
    status = "ok" if required_ok else "degraded"
    return {
        "status": status,
        "version": APP_VERSION,
        "uptime_s": round(time.time() - STARTED_AT, 3),
        "checks": checks,
        "store": store_summary,
        "llm": _llm_health(),
        "cache": cache_summary,
        "summary": {
            "checks_total": len(checks),
            "checks_ok": optional_up,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        },
    }


async def _no_config(detail: str, exc: BaseException) -> tuple[bool, str]:
    """Return a failed probe result for a missing configuration value.

    Args:
        detail: Human-readable reason.
        exc: The exception that prevented configuration.

    Returns:
        ``(False, detail)``.
    """
    return False, f"{detail}: {type(exc).__name__}"


@router.get("/metrics")
async def metrics() -> dict:
    """Expose the in-process metrics snapshot.

    Returns:
        ``{"counters", "timings", "gauges", "totals", "uptime_s", "enabled"}``
        straight from ``observability.get_metrics().snapshot()``, plus the API
        counters this layer owns.
    """
    try:
        snapshot = get_metrics().snapshot()
    except Exception as exc:  # noqa: BLE001
        snapshot = {"counters": {}, "timings": {}, "gauges": {}, "uptime_s": 0.0,
                    "error": f"{type(exc).__name__}"}
    snapshot["platform"] = APP_NAME
    snapshot["version"] = APP_VERSION
    snapshot.setdefault("uptime_s", round(time.time() - STARTED_AT, 3))
    snapshot["api"] = {
        "ws_channels": len(WS_HUB._clients),
        "upload_max_bytes": max_upload_bytes(),
        "cors_origins": cors_origins(),
    }
    return snapshot


#: Config keys surfaced (already secret-shaped) by ``GET /api/config``. Every
#: value passes through ``observability.redact`` before it leaves the process.
_CONFIG_KEYS: tuple[str, ...] = (
    "APP_NAME", "HOST", "PORT", "WORKERS", "DEBUG", "ENVIRONMENT",
    "DATABASE_URL", "REDIS_URL", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD",
    "QDRANT_URL", "MINIO_ENDPOINT", "SECRET_KEY", "OPSEC_ENABLED", "TOR_PROXY",
    "TOR_CONTROL", "TOR_CONTROL_PASSWORD", "CORRELATION_TIER", "PRIMARY_AGENTS",
    "FEATURE_PDF_EXPORT", "FEATURE_EMAIL_ENUM", "FEATURE_DARK_WEB_ALERTS",
    "SHODAN_API_KEY", "HIBP_API_KEY", "NUMVERIFY_API_KEY", "DEHASHED_EMAIL",
    "DEHASHED_API_KEY", "LLM_PROVIDER_ORDER", "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "OLLAMA_URL",
    "SIGNAL_API_KEY", "SIGNAL_API_KEYS", "AUTH_DISABLED", "CORS_ORIGINS",
    "RATE_LIMIT_RPM", "RATE_LIMIT_BURST", "WS_ALLOWED_ORIGINS",
    "MAX_UPLOAD_BYTES", "ALLOW_PRIVATE_NETWORK", "AUDIT_LOG", "REDIS_URL_ENABLED",
)


@router.get("/config")
async def config_view() -> dict:
    """Expose effective configuration with every secret masked.

    Values are read from ``config`` first and the environment second, then the
    whole structure is passed through ``observability.redact`` so any key that
    *looks* like a credential is masked even if it is not in the list above.

    Returns:
        ``{"config", "env_presence", "security", "redacted_keys"}``.
    """
    values: dict[str, Any] = {}
    config_mod = _maybe_import("config")
    if config_mod is not None:
        for key in _CONFIG_KEYS:
            if hasattr(config_mod, key):
                value = getattr(config_mod, key)
                if isinstance(value, (str, int, float, bool)) or value is None:
                    values[key] = value
                else:
                    values[key] = list(value) if isinstance(value, (list, tuple, set)) else str(type(value))
    for key in _CONFIG_KEYS:
        if key not in values and key in os.environ:
            values[key] = os.environ[key]

    redacted = redact(values)
    presence: dict[str, bool] = {}
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
                "OPENROUTER_API_KEY", "SHODAN_API_KEY", "HIBP_API_KEY",
                "NUMVERIFY_API_KEY", "DEHASHED_API_KEY", "SIGNAL_API_KEY",
                "SIGNAL_API_KEYS", "SECRET_KEY", "TOR_CONTROL_PASSWORD",
                "NEO4J_PASSWORD", "DATABASE_URL", "REDIS_URL"):
        raw = os.getenv(key) or (values.get(key) if isinstance(values.get(key), str) else "")
        presence[key] = bool(str(raw or "").strip())

    auth = _maybe_import("auth")
    try:
        auth_enabled = bool(auth.auth_enabled()) if auth and hasattr(auth, "auth_enabled") else None
    except Exception:  # noqa: BLE001
        auth_enabled = None

    return {
        "config": redacted,
        "env_presence": presence,
        "security": {
            "auth_enabled": auth_enabled,
            "max_upload_bytes": max_upload_bytes(),
            "cors_origins": cors_origins(),
            "ws_origins": ws_allowed_origins()[0],
            "rate_limit": {"rpm": _rate_settings()[0], "burst": _rate_settings()[1]},
        },
        "redacted_keys": sorted(getattr(_maybe_import("observability"),
                                       "REDACT_KEYS", frozenset())),
    }


# ── OPSEC (legacy, ported verbatim in behaviour) ─────────────────────────────


@router.get("/opsec/status")
async def opsec_status() -> dict:
    """Report OPSEC status: Tor connectivity, exit node and circuit metadata.

    Returns:
        The opsec client's ``status()`` payload, or ``{"opsec_enabled":
        False, "error": ...}`` when the client is unavailable — this endpoint
        never fails a request.
    """
    try:
        from opsec import get_opsec_client

        client = await get_opsec_client()
        return await client.status()
    except Exception as exc:  # noqa: BLE001
        log.warning("opsec status unavailable: %s", type(exc).__name__)
        return {"opsec_enabled": False, "error": str(exc)}


@router.post("/opsec/newcircuit")
async def opsec_new_circuit() -> dict:
    """Request a fresh Tor circuit (new exit node).

    Returns:
        The new circuit payload, or ``{"success": False, "error": ...}`` when
        rotation is unavailable.
    """
    try:
        from opsec import get_opsec_client

        client = await get_opsec_client()
        return await client.new_circuit()
    except Exception as exc:  # noqa: BLE001
        log.warning("opsec circuit rotation unavailable: %s", type(exc).__name__)
        return {"success": False, "error": str(exc)}


# ════════════════════════════════════════════════════════════════════════════
# Pipeline WebSocket
# ════════════════════════════════════════════════════════════════════════════


def _origin_allowed(origin: str | None, allowed: list[str], strict: bool) -> bool:
    """Check a WebSocket ``Origin`` header against the allowlist.

    Args:
        origin: Raw ``Origin`` header, or ``None`` for non-browser clients.
        allowed: Configured origins.
        strict: Whether the allowlist is enforced.

    Returns:
        ``True`` when the connection may proceed.
    """
    if not strict:
        return True
    if not origin:
        # Non-browser clients (curl, tests, service-to-service) send no Origin.
        # They still had to pass the ASGI layer; allow them.
        return True
    normalised = origin.strip().rstrip("/")
    return normalised in allowed or "*" in allowed


@ws_router.websocket("/ws/pipeline/{inv_id}")
async def ws_pipeline(websocket: WebSocket, inv_id: str) -> None:
    """Stream one investigation's pipeline trace in real time.

    Behaviour (CONTRACTS.md §10 backwards-compatibility list):
      * the path is unchanged, ``/ws/pipeline/{inv_id}``;
      * subscribing to an already-finished investigation replays a single
        ``done`` event carrying the final report, so a reconnecting client
        converges on the terminal state without replaying every step;
      * server keepalive frames (``{"type": "ping"}``) are sent every
        ``WS_HEARTBEAT_S`` seconds of silence and a client ping is echoed back;
      * every exit path — normal, error, or cancellation — unsubscribes the
        socket, so no channel entry can outlive its connection.

    Args:
        websocket: The inbound socket.
        inv_id: Investigation to subscribe to.

    Raises:
        WebSocketDisconnect: Propagated from the receive loop after cleanup.
    """
    inv_id = str(inv_id or "")[:64]
    allowed, strict = ws_allowed_origins()
    origin = websocket.headers.get("origin")
    if not _origin_allowed(origin, allowed, strict):
        log.warning("ws origin rejected inv=%s origin=%s", inv_id, origin)
        await websocket.close(code=1008, reason="origin not allowed")
        return

    await websocket.accept()
    await WS_HUB.subscribe(inv_id, websocket)

    try:
        record = None
        try:
            store = await resolve_store()
            record = await store.get_investigation(inv_id)
        except Exception:  # noqa: BLE001 — a store hiccup must not kill the socket
            record = None
        if isinstance(record, dict) and record.get("status") == "done":
            await websocket.send_text(json.dumps({
                "type": "done", "inv_id": inv_id, "ts": _now_iso(),
                "status": "done",
                "report": record.get("report"),
                "agents": record.get("agents") or [],
                "steps": record.get("steps") or [],
                "input": record.get("input"),
                "input_type": record.get("input_type"),
                "agents_activated": record.get("agents_activated") or [],
                "entities": (record.get("report") or {}).get("graph")
                if isinstance(record.get("report"), dict) else None,
                "latency_s": record.get("latency_s"),
            }, default=str))

        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=WS_HEARTBEAT_S
                )
            except asyncio.TimeoutError:
                try:
                    await websocket.send_text(json.dumps(
                        {"type": "ping", "inv_id": inv_id, "ts": _now_iso()}))
                except Exception:  # noqa: BLE001
                    break
                continue
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(message, dict) and message.get("type") == "ping":
                try:
                    await websocket.send_text(json.dumps({
                        "type": "ping", "inv_id": inv_id, "ts": _now_iso(),
                        "nonce": message.get("nonce"),
                    }))
                except Exception:  # noqa: BLE001
                    break
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — never leak a socket on error
        log.debug("ws pipeline closed for %s: %s", inv_id, type(exc).__name__)
    finally:
        await WS_HUB.unsubscribe(inv_id, websocket)


# ── Router-level wiring ──────────────────────────────────────────────────────
#
# Auth and rate limiting are applied as router-level dependencies so they cover
# every route below without repeating the decorator, and so `main.py` cannot
# accidentally mount an unguarded variant of this router.

router.dependencies.extend([Depends(auth_gate), Depends(rate_limit_dep)])