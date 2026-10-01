"""
api/deps.py — FastAPI dependencies combining auth + rate limiting (WS-SEC)
=========================================================================
This is the seam every route module in ``backend/api/`` builds on. WS-API
imports from here and should never touch :mod:`auth` or :mod:`rate_limit`
directly, so the enforcement policy stays in one place::

    from api.deps import Authed, require_investigate, validate_inv_id

    @router.post("/investigate", dependencies=[Depends(require_investigate("investigate"))])
    async def investigate(req: InvestigateRequest): ...

Three things happen on every protected request, in this order:

1. **Authenticate** — Bearer / ``X-API-Key``, enforced unless
   ``AUTH_DISABLED=true`` or ``ENVIRONMENT=development``.
2. **Authorise** — the route's scope must be held by the principal.
3. **Rate limit** — token bucket keyed on ``(principal, scope)``; over budget
   returns **429** with ``Retry-After`` and ``X-RateLimit-*`` headers.

Failures return structured JSON, never a bare traceback:

* ``401`` missing/invalid key, or 403 when auth is enabled but unconfigured.
* ``403`` valid key, missing scope.
* ``429`` bucket exhausted.

``AuthError`` from :mod:`auth` is translated to a response *inside* the
dependency rather than propagating, so an unhandled auth error can never
surface as a 500 with a traceback.

:func:`validate_inv_id` is a path validator: investigation ids are used to
build filenames and dict keys, so ``../`` or a shell metacharacter in the path
must be rejected before it reaches storage. 8–32 alphanumerics/hex/dash.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from fastapi import HTTPException, Request, Response

try:  # pragma: no cover - logging must never break import
    from observability import get_logger
except Exception:  # noqa: BLE001
    import logging

    def get_logger(name: str) -> logging.Logger:  # type: ignore[misc]
        """Minimal stdlib fallback for the WS-INFRA logger."""
        return logging.getLogger(name)


log = get_logger("signal-os.api.deps")

__all__ = [
    "INV_ID_PATTERN",
    "require_auth",
    "require_investigate",
    "require_search",
    "require_read",
    "require_opsec",
    "require_websocket",
    "principal_dep",
    "get_principal",
    "rate_limit",
    "rate_limited",
    "validate_inv_id",
    "inv_id_path",
    "Authed",
    "RateLimited",
]

#: Investigation ids are generated as 8 hex chars; longer ids are tolerated
#: (uuid4 hex, prefixed ids) but the charset stays alnum/dash/underscore so an
#: id can never contain ``/``, ``..``, or a shell metacharacter.
INV_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,32}$")

#: Rate-limit scope per logical operation.
SCOPE_INVESTIGATE = "investigate"
SCOPE_SEARCH = "search"
SCOPE_READ = "read"
SCOPE_OPSEC = "opsec"
SCOPE_WEBSOCKET = "websocket"


# ── Translation helpers ──────────────────────────────────────────────────────

def _auth_error_response(exc: Exception, *, scope: str | None = None) -> HTTPException:
    """Turn an :class:`~auth.AuthError` into the right HTTP response.

    A missing/invalid key and a missing scope are both 403-worthy, but a
    *missing* key must be 401 so clients know to authenticate at all; a
    missing scope with a valid key is 403.

    Args:
        exc: The raised :class:`~auth.AuthError`.
        scope: The scope the route required, when known.

    Returns:
        An :class:`~fastapi.HTTPException` ready to ``raise``.
    """
    message = str(exc)
    lowered = message.lower()
    status = 401
    if "scope" in lowered:
        status = 403
    elif "too many failed" in lowered:
        status = 429
    detail: dict[str, Any] = {"error": message, "auth": "failed"}
    if scope:
        detail["required_scope"] = scope
    log.warning("request refused (%s): %s", status, message)
    return HTTPException(status_code=status, detail=detail)


def _missing_config_response(scope: str | None = None) -> HTTPException:
    """Response for auth-enabled-but-no-keys-configured (fail closed)."""
    detail: dict[str, Any] = {
        "error": (
            "authentication is enabled but no SIGNAL_API_KEY / SIGNAL_API_KEYS "
            "is configured on the server"
        ),
        "auth": "misconfigured",
    }
    if scope:
        detail["required_scope"] = scope
    return HTTPException(status_code=403, detail=detail)


def _rate_limit_response(retry_after: float, scope: str) -> HTTPException:
    """429 with ``Retry-After`` and the ``X-RateLimit-*`` triple."""
    headers = {
        "Retry-After": str(max(1, int(retry_after) + 1)),
        "X-RateLimit-Scope": scope,
    }
    try:
        from rate_limit import rate_limit_headers as _hdr  # lazy: avoids a cycle

        headers.update(_hdr("anonymous", scope))
    except Exception:  # noqa: BLE001 - headers are best-effort
        pass
    headers["Retry-After"] = str(max(1, int(retry_after) + 1))
    return HTTPException(
        status_code=429,
        detail={"error": "rate limit exceeded", "scope": scope,
                "retry_after_s": round(float(retry_after), 3)},
        headers=headers,
    )


# ── Principal plumbing ───────────────────────────────────────────────────────

def get_principal(request: Request) -> dict[str, Any]:
    """Resolve the principal, reusing the value FastAPI already computed.

    Lets several dependencies on one route authenticate exactly once.

    Args:
        request: The incoming request.

    Returns:
        ``{"id", "label", "scopes", "anonymous"}``.
    """
    cached = getattr(request.state, "signal_principal", None)
    if isinstance(cached, dict):
        return cached
    from auth import principal as _principal  # lazy: avoids an import cycle

    who = _principal(request)
    try:
        request.state.signal_principal = who
    except Exception:  # noqa: BLE001 - state is a convenience, not required
        pass
    return who


async def principal_dep(request: Request) -> dict[str, Any]:
    """Dependency yielding the principal.

    Args:
        request: The incoming request.

    Returns:
        The principal dict.
    """
    return get_principal(request)


def _authenticate_or_raise(request: Request, scope: str | None = None) -> dict[str, Any]:
    """Authenticate *request*, translating any failure to an HTTP response.

    Args:
        request: The incoming request.
        scope: Scope the route requires (included in error detail).

    Returns:
        The principal dict.

    Raises:
        HTTPException: 403 when auth is enabled but no keys are configured,
            401/403 for auth+scope failures.
    """
    from auth import AuthError, auth_enabled, load_api_keys  # lazy import cycle

    # Fail closed *first*: with auth enabled and no keys configured, telling the
    # caller "invalid key" is actively misleading — the server, not the caller,
    # is misconfigured. Report that explicitly.
    if auth_enabled() and not load_api_keys():
        raise _missing_config_response(scope)

    cached = getattr(request.state, "signal_principal", None)
    if isinstance(cached, dict):
        who = cached
    else:
        try:
            who = get_principal(request)
        except AuthError as exc:
            raise _auth_error_response(exc, scope=scope) from None
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            log.exception("unexpected auth failure")
            raise _auth_error_response(AuthError(f"auth backend error: {type(exc).__name__}")) from None
    return who


# ── Rate limiting ────────────────────────────────────────────────────────────

async def rate_limited(request: Request, scope: str) -> tuple[str, dict[str, str]]:
    """Consume one token for *request* in *scope*.

    Args:
        request: The incoming request.
        scope: Rate-limit scope.

    Returns:
        ``(principal_id, headers)``.

    Raises:
        HTTPException: 429 when the bucket is empty.
    """
    from rate_limit import check as _check  # lazy: avoids an import cycle

    who = _authenticate_or_raise(request, scope)
    principal_id = str(who.get("id") or "anonymous")
    allowed, retry_after, headers = _check(principal_id, scope)
    try:
        request.state.signal_rate_limit = headers
    except Exception:  # noqa: BLE001
        pass
    if not allowed:
        log.warning(
            "rate limit exceeded principal=%s scope=%s retry_after=%.1fs",
            principal_id, scope, retry_after,
        )
        raise _rate_limit_response(retry_after, scope)
    return principal_id, headers


def rate_limit_headers_for(request: Request) -> dict[str, str]:
    """Read back the ``X-RateLimit-*`` headers computed for *request*.

    Lets a route attach them to its response::

        return payload, Response(headers=rate_limit_headers_for(request))

    Args:
        request: The incoming request.

    Returns:
        Header dict (empty when nothing was recorded).
    """
    headers = getattr(request.state, "signal_rate_limit", None)
    return dict(headers) if isinstance(headers, dict) else {}


# ── Auth dependencies ────────────────────────────────────────────────────────

async def require_auth(request: Request) -> str:
    """Authenticate and return the principal id.

    Args:
        request: The incoming request.

    Returns:
        The key's label, or ``"anonymous"`` when auth is disabled.
    """
    return str(_authenticate_or_raise(request)["id"])


def require_scope_dep(scope: str) -> Callable[[Request], Any]:
    """Build an auth+authorise dependency for *scope*.

    Args:
        scope: Required scope name.

    Returns:
        An async dependency usable with ``Depends``.
    """

    async def _dep(request: Request) -> dict[str, Any]:
        """Authenticate and enforce *scope*.

        Args:
            request: The incoming request.

        Returns:
            The principal dict.

        Raises:
            HTTPException: 401/403 on auth or scope failure.
        """
        from auth import has_scope  # lazy: avoids an import cycle

        who = _authenticate_or_raise(request, scope)
        if not has_scope(who, scope):
            from auth import client_ip as _ip  # lazy

            log.warning(
                "scope denied ip=%s path=%s principal=%s scope=%s",
                _ip(request), str(getattr(request.url, "path", ""))[:120],
                who.get("id"), scope,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": f"missing required scope: {scope}",
                    "auth": "forbidden",
                    "required_scope": scope,
                    "granted_scopes": list(who.get("scopes") or []),
                },
            )
        return who

    _dep.__name__ = f"require_{scope}"
    return _dep


def require_investigate(scope: str = SCOPE_INVESTIGATE) -> Callable[[Request], Any]:
    """Auth + rate limit for the expensive pipeline endpoints.

    Args:
        scope: Rate-limit/auth scope (defaults to ``investigate``).

    Returns:
        An async dependency usable with ``Depends``.
    """
    return _combined(scope)


def require_search(scope: str = SCOPE_SEARCH) -> Callable[[Request], Any]:
    """Auth + rate limit for search endpoints.

    Args:
        scope: Rate-limit/auth scope (defaults to ``search``).

    Returns:
        An async dependency usable with ``Depends``.
    """
    return _combined(scope)


def require_read(scope: str = SCOPE_READ) -> Callable[[Request], Any]:
    """Auth + rate limit for read endpoints.

    Args:
        scope: Rate-limit/auth scope (defaults to ``read``).

    Returns:
        An async dependency usable with ``Depends``.
    """
    return _combined(scope)


def require_opsec(scope: str = SCOPE_OPSEC) -> Callable[[Request], Any]:
    """Auth + rate limit for OPSEC endpoints (Tor circuit rotation).

    Args:
        scope: Rate-limit/auth scope (defaults to ``opsec``).

    Returns:
        An async dependency usable with ``Depends``.
    """
    return _combined(scope)


def require_websocket(scope: str = SCOPE_WEBSOCKET) -> Callable[[Request], Any]:
    """Auth + rate limit for WebSocket pipeline streams.

    Args:
        scope: Rate-limit/auth scope (defaults to ``websocket``).

    Returns:
        An async dependency usable with ``Depends``.
    """
    return _combined(scope)


def _combined(scope: str) -> Callable[[Request], Any]:
    """Build a dependency that authenticates, authorises, then rate limits.

    Args:
        scope: Rate-limit/auth scope.

    Returns:
        An async dependency usable with ``Depends``.
    """
    auth_dep = require_scope_dep(scope)

    async def _dep(request: Request) -> dict[str, Any]:
        """Run auth, scope check and rate limit in order.

        Args:
            request: The incoming request.

        Returns:
            The principal dict.

        Raises:
            HTTPException: 401/403 for auth+scope, 429 when rate limited.
        """
        who = await auth_dep(request)
        await rate_limited(request, scope)
        return who

    _dep.__name__ = f"require_{scope}_limited"
    return _dep


#: Ready-made dependencies for the common routes. ``Authed`` is a plain
#: authenticate-only dependency (any valid key, any scope) — use it on the
#: health/config probes that must stay reachable for monitoring.
Authed = require_auth
Authed.__name__ = "require_auth"

#: Read-scoped dependency for ``GET`` endpoints.
Read = require_read()
#: Investigate-scoped dependency for the pipeline endpoints.
Investigate = require_investigate()
#: Search-scoped dependency.
Search = require_search()
#: OPSEC-scoped dependency.
Opsec = require_opsec()
#: WebSocket-scoped dependency.
Websocket = require_websocket()


# ── Path validation ──────────────────────────────────────────────────────────

def validate_inv_id(value: str, *, field: str = "inv_id") -> str:
    """Validate an investigation id used in a path.

    Ids reach filenames and dict keys, so ``../`` traversal, a shell
    metacharacter, or a 10 MB id must all fail before storage sees them.

    Args:
        value: The raw path segment.
        field: Field name used in the error message.

    Returns:
        The validated id, unchanged.

    Raises:
        HTTPException: 422 when the id is malformed.
    """
    candidate = str(value or "")
    if not INV_ID_PATTERN.match(candidate):
        raise HTTPException(
            status_code=422,
            detail={
                "error": f"invalid {field}",
                "pattern": "8-32 characters, letters/digits/-/_ only",
            },
        )
    return candidate


async def inv_id_path(inv_id: str) -> str:
    """Path dependency validating ``inv_id``.

    Use as ``Depends(inv_id_path)`` so the id is checked before the handler
    runs.

    Args:
        inv_id: The raw path segment.

    Returns:
        The validated id.

    Raises:
        HTTPException: 422 when the id is malformed.
    """
    return validate_inv_id(inv_id)


#: Aliases kept for symmetry with ``CONTRACTS.md`` §8.
def require_auth_dep(request: Request) -> Any:
    """``Depends(require_api_key)`` shim documented in the contract.

    Args:
        request: The incoming request.

    Returns:
        The authenticated principal id.
    """
    return require_auth(request)  # type: ignore[return-value]


def _iter_scopes(scopes: Iterable[str]) -> list[str]:
    """Normalise a scope iterable into a clean list.

    Args:
        scopes: Raw scopes.

    Returns:
        Lower-cased, de-duplicated, non-empty scope names.
    """
    out: list[str] = []
    for scope in scopes or ():
        name = str(scope).strip().lower()
        if name and name not in out:
            out.append(name)
    return out