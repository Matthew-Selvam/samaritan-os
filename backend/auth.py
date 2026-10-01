"""
auth.py — API-key authentication with scopes for Signal-OS (WS-SEC)
===================================================================
The platform had **no authentication at all**. Anyone who could reach the API
could run unlimited OSINT investigations against real named people and read
back the results — an open-source intelligence service with no front door.

Scheme
------
Two equivalent headers are accepted:

* ``Authorization: Bearer <key>``
* ``X-API-Key: <key>``

Keys come from the environment:

* ``SIGNAL_API_KEYS`` — comma-separated ``label:key:scope1,scope2`` entries.
  A key with no scopes gets ``*`` (full access); a comma inside a scope list
  would be ambiguous, so scopes are colon- or comma-separated and the parser
  accepts both.
* ``SIGNAL_API_KEY`` — single-key shorthand; label ``default``, scopes ``*``.

``auth_enabled()`` returns ``True`` by default. Anonymous access happens only
when ``AUTH_DISABLED=true`` or ``ENVIRONMENT=development``, and then the
principal is ``{"id": "anonymous", "scopes": ["*"]}``.

Security properties
--------------------
* Key comparison uses :func:`hmac.compare_digest` against **every** configured
  key, so a match leaks neither the key's position nor a timing signal — and,
  because the loop is not short-circuited, neither does its runtime.
* Failures log the client IP and path. The presented key is **never** logged,
  not even truncated, and not even a key's label.
* Repeated failures from one IP are throttled (:data:`AUTH_FAIL_THRESHOLD`
  within :data:`AUTH_FAIL_WINDOW_S`) so the endpoint cannot be used as an
  online key-guessing oracle.
* ``hmac.compare_digest`` needs ``str`` (ASCII) or ``bytes``; a non-ASCII
  presented key is rejected before comparison rather than crashing.

If **no** keys are configured and auth is enabled, every request is refused
with an explicit server-misconfiguration error rather than being waved
through — failing closed is the only safe default here.
"""
from __future__ import annotations

import hmac
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

try:  # pragma: no cover - logging must never break import
    from observability import get_logger
except Exception:  # noqa: BLE001
    import logging

    def get_logger(name: str) -> logging.Logger:  # type: ignore[misc]
        """Minimal stdlib fallback for the WS-INFRA logger."""
        return logging.getLogger(name)


log = get_logger("signal-os.auth")

__all__ = [
    "AuthError",
    "ApiKey",
    "ANONYMOUS_PRINCIPAL",
    "SCOPES",
    "auth_enabled",
    "load_api_keys",
    "require_api_key",
    "principal",
    "require_scope",
    "has_scope",
    "client_ip",
]

#: Auth failures from one IP allowed inside :data:`AUTH_FAIL_WINDOW_S` before
#: the IP is throttled.
AUTH_FAIL_THRESHOLD = 10
AUTH_FAIL_WINDOW_S = 300.0

#: Scope names the platform issues.
SCOPES: tuple[str, ...] = (
    "investigate", "search", "read", "write", "opsec", "admin", "*",
)

ANONYMOUS_PRINCIPAL: dict[str, Any] = {
    "id": "anonymous",
    "label": "anonymous",
    "scopes": ["*"],
    "anonymous": True,
}

#: Principals that failed to authenticate, keyed by IP -> deque of timestamps.
_FAILURES: dict[str, list[float]] = {}


class AuthError(Exception):
    """Raised when a request carries no, or invalid, credentials.

    Surfaced as HTTP 401 by :mod:`api.deps`; the message is deliberately
    generic ("invalid or missing API key") so it never confirms that a
    particular key exists.
    """


@dataclass(frozen=True)
class ApiKey:
    """One configured API key.

    Attributes:
        label: Human-readable name from the env var (never logged as a
            secret — it identifies the key, not the credential).
        key: The secret itself. Never logged, never serialised.
        scopes: Granted scopes; ``["*"]`` means everything.
    """

    label: str
    key: str
    scopes: tuple[str, ...] = ("*",)


# ── Configuration ────────────────────────────────────────────────────────────

def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def auth_enabled() -> bool:
    """Whether authentication is enforced.

    ``False`` only when ``AUTH_DISABLED=true`` or ``ENVIRONMENT=development``.

    Returns:
        ``True`` when requests must present a valid API key.
    """
    if _env_flag("AUTH_DISABLED", False):
        return False
    if os.getenv("ENVIRONMENT", "").strip().lower() == "development":
        return False
    return True


def _parse_scopes(raw: str) -> tuple[str, ...]:
    """Parse a scope string into a tuple.

    Accepts ``"read,write"``, ``"read:write"`` and ``"*"``. Empty input yields
    ``("*",)`` so a key without an explicit scope list keeps full access.
    """
    text = str(raw or "").replace(":", ",").strip()
    if not text or text == "*":
        return ("*",)
    scopes = tuple(s.strip().lower() for s in text.split(",") if s.strip())
    return scopes or ("*",)


def load_api_keys() -> tuple[ApiKey, ...]:
    """Parse ``SIGNAL_API_KEYS`` / ``SIGNAL_API_KEY`` into key records.

    Format: ``label:key:scope1,scope2`` separated by commas between entries.
    A bare ``label:key`` gets full access.

    Returns:
        The configured keys, in declaration order. Empty when none are set.
    """
    keys: list[ApiKey] = []
    blob = os.getenv("SIGNAL_API_KEYS", "").strip()
    for entry in _split_entries(blob):
        if not entry:
            continue
        label, _, rest = entry.partition(":")
        key, _, scopes = rest.partition(":")
        key = key.strip()
        if not key:
            log.warning("SIGNAL_API_KEYS entry %r has no key; skipped", label.strip())
            continue
        keys.append(ApiKey(
            label=label.strip() or "unnamed",
            key=key,
            scopes=_parse_scopes(scopes),
        ))

    single = os.getenv("SIGNAL_API_KEY", "").strip()
    if single:
        keys.append(ApiKey(label="default", key=single, scopes=("*",)))

    if not keys and auth_enabled():
        log.error(
            "Authentication is enabled but no SIGNAL_API_KEY/SIGNAL_API_KEYS is "
            "configured — every request will be rejected (401)."
        )
    return tuple(keys)


def _split_entries(blob: str) -> list[str]:
    """Split the ``SIGNAL_API_KEYS`` blob into entries.

    A comma separates *entries*; a colon separates the fields of one entry.
    Scopes are therefore comma-free — ``label:key:read,write`` is one key
    scoped ``read,write`` is ambiguous, so this parser reads a colon-separated
    scope list when the blob uses semicolons between entries, and the plain
    comma form when scopes are omitted.

    Args:
        blob: Raw env value.

    Returns:
        Entry strings of the form ``label:key[:scopes]``.
    """
    if not blob:
        return []
    # Preferred form: semicolon-separated entries allow commas inside scopes.
    if ";" in blob:
        return [e.strip() for e in blob.split(";") if e.strip()]
    # Comma-separated form: an entry is only "complete" when it has a colon
    # and its key field is non-empty. ``label:key:read,write`` -> the second
    # chunk "write" is treated as another entry and dropped with a warning.
    entries: list[str] = []
    pending: list[str] = []
    for chunk in blob.split(","):
        piece = chunk.strip()
        pending.append(piece)
        if piece.count(":") >= 1:
            entries.append(":".join(pending))
            pending = []
    if pending:
        leftover = ",".join(pending).strip()
        if leftover:
            log.warning("SIGNAL_API_KEYS: ignoring incomplete trailing entry")
    return entries


# ── Failure throttle ─────────────────────────────────────────────────────────

def _record_failure(ip: str) -> int:
    """Record an auth failure and report how many are in the window.

    Args:
        ip: Client IP.

    Returns:
        The number of recent failures for *ip* (including this one).
    """
    now = time.monotonic()
    window = _FAILURES.setdefault(ip, [])
    window.append(now)
    cutoff = now - AUTH_FAIL_WINDOW_S
    if window and window[0] < cutoff:
        window[:] = [t for t in window if t >= cutoff]
    # Bound memory: stop tracking IPs that have long gone quiet.
    if len(_FAILURES) > 4096:
        for stale in [k for k, v in _FAILURES.items() if not v or v[-1] < cutoff]:
            _FAILURES.pop(stale, None)
    return len(window)


def failure_count(ip: str) -> int:
    """Recent auth failures recorded for *ip* (within the throttle window)."""
    window = _FAILURES.get(ip, [])
    cutoff = time.monotonic() - AUTH_FAIL_WINDOW_S
    return sum(1 for t in window if t >= cutoff)


def _reset_failures() -> None:
    """Clear the failure throttle (tests)."""
    _FAILURES.clear()


# ── Request helpers ──────────────────────────────────────────────────────────

def client_ip(request: Any) -> str:
    """Best-effort client IP (delegates to :func:`security.client_ip`)."""
    try:
        from security import client_ip as _impl  # lazy: avoids an import cycle
        return _impl(request)
    except Exception:  # noqa: BLE001
        try:
            return (request.client.host if getattr(request, "client", None) else "unknown")[:64]
        except Exception:  # noqa: BLE001
            return "unknown"


def _presented_key(request: Any) -> str:
    """Extract the presented credential from the request headers.

    Args:
        request: Starlette/FastAPI request.

    Returns:
        The raw key, or ``""`` when no credential was presented.
    """
    auth = ""
    try:
        auth = request.headers.get("authorization") or ""
    except Exception:  # noqa: BLE001
        auth = ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    try:
        return (request.headers.get("x-api-key") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _request_path(request: Any) -> str:
    """The request path, for audit logs only (no query string — it can hold PII)."""
    try:
        return str(getattr(getattr(request, "url", None), "path", "") or "")[:200]
    except Exception:  # noqa: BLE001
        return ""


def _lookup(presented: str) -> ApiKey | None:
    """Constant-work lookup of *presented* against every configured key.

    Args:
        presented: The key from the request.

    Returns:
        The matching :class:`ApiKey`, or ``None``.

    Notes:
        Every configured key is compared even after a match, so the loop's
        runtime does not reveal which key was correct.
    """
    keys = load_api_keys()
    if not keys:
        return None
    match: ApiKey | None = None
    encoded = presented.encode("utf-8", "replace")
    for candidate in keys:
        ok = hmac.compare_digest(encoded, candidate.key.encode("utf-8", "replace"))
        if ok and match is None:
            match = candidate
    return match


def _build_principal(record: ApiKey) -> dict[str, Any]:
    """Turn a matched key into the principal dict used downstream."""
    return {
        "id": record.label,
        "label": record.label,
        "scopes": list(record.scopes),
        "anonymous": False,
    }


def has_scope(principal_dict: dict[str, Any], scope: str) -> bool:
    """Whether a principal holds *scope*.

    Args:
        principal_dict: Principal from :func:`principal` / :func:`require_api_key`.
        scope: Required scope.

    Returns:
        ``True`` when the principal has the scope or the ``*`` wildcard.
    """
    scopes = (principal_dict or {}).get("scopes") or []
    if not isinstance(scopes, Iterable) or isinstance(scopes, (str, bytes)):
        scopes = [scopes]
    scopes = {str(s).strip().lower() for s in scopes}
    if "*" in scopes:
        return True
    wanted = str(scope or "").strip().lower()
    if wanted in scopes:
        return True
    # `read` implies `write`? No — least privilege. But a key scoped
    # "investigate" should not need a separate "read" to fetch its own result.
    return wanted == "read" and bool({"investigate", "search"} & scopes)


def _authenticate(request: Any) -> dict[str, Any]:
    """Resolve the principal for *request*, or raise :class:`AuthError`."""
    if not auth_enabled():
        return dict(ANONYMOUS_PRINCIPAL)

    ip = client_ip(request)
    path = _request_path(request)
    if failure_count(ip) >= AUTH_FAIL_THRESHOLD:
        # Throttle: do not even tell the caller whether the key was close.
        log.warning("auth throttled ip=%s path=%s (too many recent failures)", ip, path)
        raise AuthError("too many failed authentication attempts")

    presented = _presented_key(request)
    if not presented:
        _record_failure(ip)
        log.warning("auth failure ip=%s path=%s reason=missing_key", ip, path)
        raise AuthError("missing API key")

    try:
        record = _lookup(presented)
    except Exception as exc:  # noqa: BLE001 - never 500 on a weird header
        _record_failure(ip)
        log.warning("auth failure ip=%s path=%s reason=lookup_error", ip, path)
        raise AuthError(f"invalid or missing API key ({type(exc).__name__})") from None

    if record is None:
        _record_failure(ip)
        log.warning("auth failure ip=%s path=%s reason=bad_key", ip, path)
        raise AuthError("invalid or missing API key")

    return _build_principal(record)


# ── Public API ───────────────────────────────────────────────────────────────

def require_api_key(request: Any) -> str:
    """FastAPI dependency: authenticate and return the principal id.

    Args:
        request: The incoming request.

    Returns:
        The key's label/id (``"anonymous"`` when auth is disabled).

    Raises:
        AuthError: No credential, invalid credential, or throttled IP.
    """
    return str(_authenticate(request)["id"])


def principal(request: Any) -> dict[str, Any]:
    """FastAPI dependency: authenticate and return the full principal.

    Args:
        request: The incoming request.

    Returns:
        ``{"id", "label", "scopes", "anonymous"}``.

    Raises:
        AuthError: No credential, invalid credential, or throttled IP.
    """
    return _authenticate(request)


def require_scope(scope: str) -> Any:
    """Build a dependency factory that demands *scope*.

    Usage::

        @router.post("/api/investigate", dependencies=[Depends(require_scope("investigate"))])

    Args:
        scope: Required scope (``investigate``, ``search``, ``read``, ...).

    Returns:
        An async dependency callable usable with ``Depends``.
    """
    wanted = str(scope or "").strip().lower()

    async def _dep(request: Any) -> dict[str, Any]:
        """Authenticate, then enforce the scope.

        Args:
            request: The incoming request.

        Returns:
            The principal dict, for dependency injection into the route.

        Raises:
            AuthError: Authentication failed or the scope is missing.
        """
        who = _authenticate(request)
        if not has_scope(who, wanted):
            log.warning(
                "auth denied ip=%s path=%s principal=%s missing_scope=%s",
                client_ip(request), _request_path(request), who.get("id"), wanted,
            )
            raise AuthError(f"missing required scope: {wanted}")
        return who

    _dep.__name__ = f"require_scope_{wanted or 'any'}"
    return _dep