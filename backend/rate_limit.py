"""
rate_limit.py — Token-bucket rate limiting for Signal-OS (WS-SEC)
==================================================================
Before this module existed, anyone who could reach the API could run unlimited
OSINT investigations against real people — an unbounded bill, an unbounded
outbound request blast toward third-party sources, and an easy way to get the
deployment's IP (or the operator's Tor exit) banned.

The limiter is a **token bucket per (principal, scope)**: ``rate`` is the
sustained requests-per-minute, ``burst`` the bucket depth. Scopes exist because
a uniform limit is useless — ``POST /api/investigate`` fans out to ~15 agents
and dozens of outbound HTTP calls, while ``GET /api/health`` is nearly free.
Limiting them together would either break the app or break the health check.

Scopes:
    investigate  — full multi-agent pipeline. Lowest RPM by far.
    search       — SCOUT / federation / dorks.
    read         — GETs over stored results.
    opsec        — Tor circuit rotation.
    websocket    — pipeline streaming connections.

State is in-process (no Redis round-trip on the hot path, which keeps the
limiter working when Redis is absent — the app must run with zero
infrastructure). That means the limit is **per worker process**: with
``WORKERS=4`` the effective ceiling is 4x the configured rate. Redis-backed
shared counting can be added later behind the same API.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

try:  # pragma: no cover - logging must never break import
    from observability import get_logger
except Exception:  # noqa: BLE001
    import logging

    def get_logger(name: str) -> logging.Logger:  # type: ignore[misc]
        """Minimal stdlib fallback for the WS-INFRA logger."""
        return logging.getLogger(name)


log = get_logger("signal-os.rate-limit")

__all__ = [
    "RateLimiter",
    "ScopeConfig",
    "SCOPES",
    "get_limiter",
    "rate_limit_headers",
    "check",
    "reset_all",
    "default_rpm",
    "default_burst",
]

#: Sustained requests-per-minute per scope when ``RATE_LIMIT_SCOPE_RPM`` does
#: not override it. ``investigate`` is intentionally an order of magnitude
#: below the cheap scopes.
DEFAULT_SCOPE_RPM: dict[str, int] = {
    "investigate": 6,
    "search": 30,
    "read": 240,
    "opsec": 12,
    "websocket": 30,
    "default": 60,
}

#: Bucket depth per scope. A burst equal to the per-minute rate means "a full
#: minute of idle budget may be spent at once".
DEFAULT_SCOPE_BURST: dict[str, int] = {
    "investigate": 2,
    "search": 10,
    "read": 120,
    "opsec": 3,
    "websocket": 10,
    "default": 30,
}

#: Every scope name the app recognises.
SCOPES: tuple[str, ...] = tuple(DEFAULT_SCOPE_RPM)

#: Cap on tracked (principal, scope) buckets, so an attacker rotating fake
#: principal ids cannot grow the dict without bound.
MAX_BUCKETS = 20_000


def _env_int(name: str, default: int) -> int:
    """Read a positive int env var, falling back on junk."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def default_rpm(scope: str = "default") -> int:
    """Sustained requests-per-minute for *scope*.

    Precedence: ``RATE_LIMIT_SCOPE_RPM`` (per-scope overrides, e.g.
    ``RATE_LIMIT_SCOPE_RPM.investigate=3``) → ``RATE_LIMIT_RPM`` (global) →
    the built-in table.

    Args:
        scope: Scope name.

    Returns:
        Requests per minute.
    """
    per_scope = os.getenv(f"RATE_LIMIT_SCOPE_RPM.{scope}")
    if per_scope:
        return _env_int(f"RATE_LIMIT_SCOPE_RPM.{scope}", DEFAULT_SCOPE_RPM.get(scope, 60))
    global_rpm = _env_int("RATE_LIMIT_RPM", 0)
    if global_rpm:
        return global_rpm
    return DEFAULT_SCOPE_RPM.get(scope, DEFAULT_SCOPE_RPM["default"])


def default_burst(scope: str = "default") -> int:
    """Bucket depth for *scope*.

    Precedence mirrors :func:`default_rpm`.

    Args:
        scope: Scope name.

    Returns:
        Maximum tokens in the bucket.
    """
    per_scope = os.getenv(f"RATE_LIMIT_BURST.{scope}")
    if per_scope:
        return _env_int(f"RATE_LIMIT_BURST.{scope}", DEFAULT_SCOPE_BURST.get(scope, 30))
    global_burst = _env_int("RATE_LIMIT_BURST", 0)
    if global_burst:
        return global_burst
    return DEFAULT_SCOPE_BURST.get(scope, DEFAULT_SCOPE_BURST["default"])


@dataclass
class ScopeConfig:
    """Resolved limits for one scope.

    Attributes:
        scope: Scope name.
        rate: Sustained requests per minute.
        burst: Bucket depth.
    """

    scope: str
    rate: float
    burst: int

    @property
    def per_second(self) -> float:
        """Tokens added per second."""
        return self.rate / 60.0


@dataclass
class _Bucket:
    """Mutable token-bucket state for one (principal, scope) pair."""

    tokens: float
    updated: float
    seen: int = 0


class RateLimiter:
    """A token bucket over ``rate`` requests/minute with a ``burst`` depth.

    Args:
        key: Bucket identity (usually ``"{principal}:{scope}"``).
        rate: Sustained requests per minute.
        burst: Bucket depth. Defaults to ``max(1, rate)``.

    Attributes:
        key: The bucket key.
        rate: Configured requests per minute.
        burst: Configured bucket depth.
    """

    def __init__(self, key: str, rate: float, burst: int | None = None) -> None:
        self.key = str(key)
        self.rate = float(max(rate, 0.0001))
        self.burst = int(burst if burst is not None else max(1, round(self.rate)))
        self._bucket = _Bucket(tokens=float(self.burst), updated=time.monotonic())

    # ── internals ────────────────────────────────────────────────────────
    def _refill(self) -> float:
        """Add the tokens accrued since the last call.

        Returns:
            The bucket's current token count.
        """
        now = time.monotonic()
        elapsed = max(0.0, now - self._bucket.updated)
        self._bucket.updated = now
        self._bucket.tokens = min(
            float(self.burst), self._bucket.tokens + elapsed * (self.rate / 60.0)
        )
        return self._bucket.tokens

    # ── public API ───────────────────────────────────────────────────────
    def allow(self, cost: float = 1.0) -> tuple[bool, float]:
        """Consume one token (or ``cost``) if available.

        Args:
            cost: Tokens to consume.

        Returns:
            ``(allowed, retry_after_s)``. ``retry_after_s`` is ``0.0`` when the
            request was allowed, otherwise the seconds until enough tokens
            exist again.
        """
        if self.rate <= 0:  # limiter disabled for this scope
            return True, 0.0
        available = self._refill()
        if available >= cost:
            self._bucket.tokens = available - cost
            self._bucket.seen += 1
            return True, 0.0
        deficit = cost - available
        retry_after = deficit / (self.rate / 60.0) if self.rate > 0 else 1.0
        # Never promise a shorter wait than a full bucket drain: when only a
        # fraction of a token is missing, waiting for the whole token is what
        # actually admits the next request.
        return False, max(0.001, round(retry_after, 3))

    def seconds_until(self, cost: float = 1.0) -> float:
        """Seconds until *cost* tokens are available.

        Args:
            cost: Tokens needed.

        Returns:
            The wait in seconds (0.0 when already available).
        """
        if self.rate <= 0:
            return 0.0
        deficit = cost - self._refill()
        if deficit <= 0:
            return 0.0
        return round(deficit / (self.rate / 60.0), 3)

    @property
    def remaining(self) -> float:
        """Tokens currently available (floored at 0)."""
        return max(0.0, self._refill())

    def reset(self) -> None:
        """Refill the bucket to full."""
        self._bucket = _Bucket(tokens=float(self.burst), updated=time.monotonic())

    def headers(self) -> dict[str, str]:
        """Standard ``X-RateLimit-*`` headers for this bucket.

        Returns:
            ``X-RateLimit-Limit`` / ``-Remaining`` / ``-Reset`` (and
            ``-Retry-After`` when currently empty).
        """
        remaining = self.remaining
        reset = int(self.seconds_until(remaining)) if self.rate else 0
        out = {
            "X-RateLimit-Limit": str(int(self.rate)),
            "X-RateLimit-Remaining": str(int(remaining)),
            "X-RateLimit-Reset": str(reset),
        }
        if remaining < 1:
            # ``allow(cost=0)`` would report a fractional-token wait; report the
            # wait for a whole token instead so the header is honest.
            out["X-RateLimit-Retry-After"] = str(self.seconds_until(1.0))
        return out


# ── Registry ─────────────────────────────────────────────────────────────────

#: scope -> {principal -> RateLimiter}
_BUCKETS: dict[str, dict[str, RateLimiter]] = {scope: {} for scope in SCOPES}


def get_limiter(scope: str, principal_id: str = "anonymous") -> RateLimiter:
    """Return the token bucket for ``(principal_id, scope)``, creating it once.

    Args:
        scope: One of :data:`SCOPES` (unknown scopes fall back to ``default``).
        principal_id: Authenticated principal, or ``anonymous``.

    Returns:
        A memoized :class:`RateLimiter`.
    """
    key = str(principal_id or "anonymous")[:128]
    scope_key = scope if scope in _BUCKETS else "default"
    table = _BUCKETS[scope_key]
    bucket_key = f"{key}:{scope_key}"
    limiter = table.get(bucket_key)
    if limiter is None:
        if len(table) >= MAX_BUCKETS:  # bound memory against id-rotation
            oldest = min(table, key=lambda k: table[k]._bucket.seen)  # noqa: SLF001
            table.pop(oldest, None)
            log.warning("rate-limit bucket table full; evicted %s", oldest)
        limiter = RateLimiter(
            bucket_key,
            rate=float(default_rpm(scope_key)),
            burst=default_burst(scope_key),
        )
        table[bucket_key] = limiter
    return limiter


def check(principal_id: str, scope: str, cost: float = 1.0) -> tuple[bool, float, dict[str, str]]:
    """Consume a token for a request and produce its response headers.

    Args:
        principal_id: Authenticated principal, or ``anonymous``.
        scope: Rate-limit scope.
        cost: Tokens to consume.

    Returns:
        ``(allowed, retry_after_s, headers)``.
    """
    limiter = get_limiter(scope, principal_id)
    allowed, retry_after = limiter.allow(cost)
    headers = limiter.headers()
    if not allowed:
        # Report the whole-token wait (Retry-After is seconds, integer) rather
        # than the possibly-fractional figure from the last attempt.
        retry_after = limiter.seconds_until(max(cost, 1.0))
        headers["Retry-After"] = str(max(1, int(retry_after) + 1))
        headers["X-RateLimit-Retry-After"] = str(retry_after)
    return allowed, retry_after, headers


def rate_limit_headers(principal_id: str, scope: str) -> dict[str, str]:
    """``X-RateLimit-*`` headers without consuming a token.

    Args:
        principal_id: Authenticated principal, or ``anonymous``.
        scope: Rate-limit scope.

    Returns:
        Header dict.
    """
    try:
        return get_limiter(scope, principal_id).headers()
    except Exception as exc:  # noqa: BLE001 - headers are best-effort
        log.debug("rate_limit_headers failed: %s", exc)
        return {}


def reset_all() -> None:
    """Refill every tracked bucket (used by tests and by the health endpoint)."""
    for table in _BUCKETS.values():
        for limiter in table.values():
            limiter.reset()