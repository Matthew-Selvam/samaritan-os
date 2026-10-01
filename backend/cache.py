"""
cache.py — TTL Cache with Redis / In-Memory Fallback
====================================================
One cache API for the whole platform, with two interchangeable backends:

* **Redis** — used when ``REDIS_URL`` is set *and* a live PING succeeds.
* **In-process TTL-LRU dict** — the default. Zero infrastructure, no import
  of any third-party package unless the backends are actually used.

Selection is lazy and cached: the first ``cache_get``/``cache_set`` resolves
the backend, and a failed probe permanently (for the process, or until
:func:`invalidate_backend`) drops to memory. A Redis outage therefore degrades
to "slower but working", never to an exception — **every** public function
here is exception-proof and returns a safe value instead of raising.

Values are JSON-encoded before storage so the two backends are
interchangeable and a cached payload can never hold an unpicklable object.

Usage::

    @cached(ttl=300, key="shodan:ip:{ip}")
    async def lookup(ip: str) -> dict: ...
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Iterable

import config
from observability import get_logger, get_metrics

__all__ = [
    "cached",
    "cache_backend",
    "cache_backend_async",
    "cache_delete",
    "cache_delete_prefix",
    "cache_get",
    "cache_set",
    "cache_stats",
    "invalidate_backend",
]


log = get_logger("signal-os.cache")

#: Marker stored for a cached ``None`` so a legitimately-empty result is not
#: re-computed on every call.
_NULL = "\x00__cache_null__\x00"

_MAX_JSON_BYTES = 1_000_000  # don't store absurd payloads in Redis


# ── In-memory backend ────────────────────────────────────────────────────────

class _MemoryCache:
    """TTL + LRU map. Bounded, lock-protected, and self-pruning."""

    def __init__(self, max_size: int) -> None:
        """Create the map.

        Args:
            max_size: Maximum number of live entries; the least recently used
                is evicted once the bound is exceeded.
        """
        self.max_size = max(1, int(max_size or 1))
        self._data: "OrderedDict[str, tuple[Any, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[bool, Any]:
        """Look up a live entry.

        Returns:
            ``(found, value)``; ``(False, None)`` when missing or expired.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False, None
            value, expires_at = entry
            if expires_at and expires_at <= now:
                del self._data[key]
                return False, None
            self._data.move_to_end(key)
            return True, value

    def set(self, key: str, value: Any, ttl: float) -> None:
        """Store a value with an optional TTL (≤ 0 means it never expires)."""
        expires_at = time.monotonic() + ttl if ttl and ttl > 0 else 0.0
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (value, expires_at)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)  # evict LRU

    def delete(self, key: str) -> int:
        """Delete exactly one key; return 1 if it existed, else 0."""
        with self._lock:
            return 1 if self._data.pop(key, None) is not None else 0

    def delete_prefix(self, prefix: str) -> int:
        """Delete every key starting with ``prefix``; return how many went."""
        with self._lock:
            doomed = [k for k in self._data if k.startswith(prefix)]
            for key in doomed:
                del self._data[key]
            return len(doomed)

    def clear(self) -> None:
        """Drop every entry."""
        with self._lock:
            self._data.clear()

    def size(self) -> int:
        """Return the number of live (unexpired) entries."""
        now = time.monotonic()
        with self._lock:
            stale = [k for k, (_, exp) in self._data.items() if exp and exp <= now]
            for key in stale:
                del self._data[key]
            return len(self._data)


#: The always-present in-process backend. Allocated eagerly (it is pure
#: stdlib and costs a dict) so the Redis outage path has zero latency and no
#: code path can observe an uninitialised cache.
_memory = _MemoryCache(getattr(config, "CACHE_MAX_SIZE", 1000))


# ── Backend resolution ───────────────────────────────────────────────────────

_backend: str | None = None          # "redis" | "memory" | None (unresolved)
_redis: Any = None                    # redis.asyncio.Redis client
_backend_lock = threading.Lock()

_stats_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0, "sets": 0, "deletes": 0, "errors": 0, "key_hits": {}}


def _record_hit() -> None:
    with _stats_lock:
        _stats["hits"] += 1
    get_metrics().incr("cache.hit")


def _record_miss() -> None:
    with _stats_lock:
        _stats["misses"] += 1
    get_metrics().incr("cache.miss")


async def cache_backend_async() -> str:
    """Return the active backend name, resolving it on first use.

    The probe is performed *outside* ``_backend_lock``. Doing it while holding
    the lock was a self-deadlock: with no running event loop the probe calls
    ``asyncio.run(...)``, whose failure path (``_ping_async``) re-acquired the
    same lock to drop to the memory backend. Because that lock is not
    reentrant, a synchronous first call with Redis down blocked forever —
    a health endpoint that never returned. Resolving before taking the lock
    also stops concurrent callers from queueing behind a network timeout.

    The probe is awaited because its verdict must be known before committing
    to a backend: scheduling it and assuming success reported "redis" for a
    server that was not answering.

    Returns:
        ``"redis"`` when a live Redis was reached, otherwise ``"memory"``.
    """
    global _backend
    if _backend is not None:
        return _backend

    resolved = "redis" if await _probe_redis() else "memory"
    with _backend_lock:
        if _backend is None:
            if resolved == "memory":
                log.info(
                    "cache backend: in-process TTL-LRU (max_size=%d)",
                    getattr(config, "CACHE_MAX_SIZE", 1000),
                )
            _backend = resolved
        return _backend


def cache_backend() -> str:
    """Return the active backend name from synchronous code.

    Once resolved, this is a cheap attribute read and never probes. When the
    backend has not been resolved yet, the probe is run to completion on a
    private loop — the caller is assumed to be a synchronous entry point (an
    ops report, a CLI) rather than an event-loop task, so nothing is blocked.
    Async callers should use :func:`cache_backend_async` instead.

    Returns:
        ``"redis"`` when a live Redis was reached, otherwise ``"memory"``.
    """
    global _backend
    if _backend is not None:
        return _backend
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Genuinely synchronous caller: safe to block on the probe.
        return asyncio.run(cache_backend_async())

    # Inside a loop we must not block it. Probe on a private worker thread so
    # the caller still gets a truthful answer instead of an optimistic guess.
    import threading

    box: dict[str, str] = {}

    def _worker() -> None:
        box["backend"] = asyncio.run(cache_backend_async())

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=getattr(config, "REDIS_PROBE_TIMEOUT", 5.0) or 5.0)
    return box.get("backend", _backend or "memory")


async def _probe_redis() -> bool:
    """Try to reach Redis once. Never raises.

    Honours ``REDIS_REQUIRED``: when true an unreachable Redis is logged as
    an error rather than a routine fallback, so a misconfigured deployment is
    visible in the logs.

    ``redis.asyncio`` clients are coroutine-based: calling ``ping()`` from
    synchronous code returns an un-awaited coroutine instead of performing a
    round trip, so a naive probe *always* succeeds and every later operation
    then fails. The probe therefore has to happen on the event loop.

    Returns:
        ``True`` when a usable ``redis.asyncio`` client was obtained.
    """
    url = getattr(config, "REDIS_URL", "") or ""
    if not url:
        return False
    try:
        import redis.asyncio as aioredis  # lazy: never at module scope

        client = aioredis.from_url(
            url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
            health_check_interval=30,
        )
    except Exception as exc:  # noqa: BLE001 — an absent Redis is expected
        return _probe_failed(exc)

    # Perform the round trip on the loop and *await the verdict* before
    # declaring the backend live. Scheduling the ping and returning `True`
    # unconditionally (an earlier version) meant a dead Redis still reported
    # "redis", so the caller committed to a backend that could not answer.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Synchronous caller (e.g. cache_stats()): run the ping on a bounded
        # private loop so the probe is still a real network round trip.
        try:
            ok = asyncio.run(_ping_async(client))
        except Exception as exc:  # noqa: BLE001
            _close_quietly(client)
            return _probe_failed(exc)
    else:
        ok = await _ping_async(client)

    if not ok:
        return False

    global _redis
    _redis = client
    log.info("cache backend: redis")
    return True


async def _ping_async(client) -> bool:
    """Ping Redis, dropping to the memory backend if it does not answer.

    Args:
        client: An initialised ``redis.asyncio`` client.

    Returns:
        ``True`` when the ping succeeded.
    """
    global _redis, _backend
    try:
        await client.ping()
        return True
    except Exception as exc:  # noqa: BLE001 — Redis down is an expected mode
        _close_quietly(client)
        # Reassignment happens outside the lock: this coroutine can run inside
        # the probe that _probe_redis() started on a thread with no event loop,
        # where taking the same non-reentrant lock would self-deadlock. The
        # writes are single attribute rebinds, so they are safe here, and
        # cache_backend() re-checks _backend under the lock before committing.
        if _redis is client:
            _redis = None
            _backend = "memory"
        _probe_failed(exc)
        return False


def _close_quietly(client) -> None:
    """Close a Redis client without ever raising."""
    if client is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop is not None:
            loop.create_task(client.aclose())
        else:
            asyncio.run(client.aclose())
    except Exception:  # noqa: BLE001 — best effort only
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _probe_failed(exc: BaseException) -> bool:
    """Record a failed Redis probe and return ``False`` (memory backend).

    Args:
        exc: The exception that caused the probe to fail.

    Returns:
        Always ``False``, so callers can ``return _probe_failed(exc)``.
    """
    global _stats
    with _stats_lock:
        _stats["errors"] += 1
    level = log.error if getattr(config, "REDIS_REQUIRED", False) else log.info
    level("redis unavailable (%s) — using in-process cache", type(exc).__name__)
    return False


def invalidate_backend() -> None:
    """Force the next call to re-probe the backend. Intended for tests."""
    global _backend, _redis
    with _backend_lock:
        _redis = None
        _backend = None


def _demote_to_memory() -> None:
    """Drop to the in-process backend after a live Redis failure.

    A Redis outage *after* a successful probe (failover, network partition,
    maxmemory eviction storm) must not silently turn every subsequent read
    into a miss and every write into a no-op. Once demoted, the process keeps
    serving from memory until :func:`invalidate_backend` re-probes.
    """
    global _backend, _redis
    with _backend_lock:
        if _backend == "redis":
            _backend = "memory"
            log.warning("redis became unusable — demoted to in-process cache")
            stale, _redis = _redis, None
        else:
            stale = None
    if stale is not None:
        _close_quietly(stale)


async def _disconnect_redis() -> None:
    """Close the Redis client if one is open. Never raises."""
    global _redis
    client, _redis = _redis, None
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 — some redis versions lack aclose()
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


# ── Serialization ────────────────────────────────────────────────────────────

def _encode(value: Any) -> str | None:
    """JSON-encode a value for storage.

    Returns:
        The JSON string, or ``None`` when the value is not JSON-serialisable
        (the caller then skips caching rather than raising).
    """
    try:
        if value is None:
            return _NULL
        blob = json.dumps(value, default=str)
        if len(blob) > _MAX_JSON_BYTES:
            log.debug("cache: value too large to store (%d bytes)", len(blob))
            return None
        return blob
    except (TypeError, ValueError, RecursionError) as exc:
        log.debug("cache: unserialisable value (%s)", type(exc).__name__)
        return None


def _decode(blob: Any) -> Any:
    """JSON-decode a stored value, tolerating corruption.

    Returns:
        The decoded value, or ``None`` if the blob is unreadable. A stored
        null-marker decodes back to ``None`` (a real cache miss on the next
        read, which is the safe direction).
    """
    if blob is None:
        return None
    try:
        if blob == _NULL:
            return None
        return json.loads(blob)
    except (TypeError, ValueError):
        log.warning("cache: dropping corrupt entry")
        return None


# ── Public API ───────────────────────────────────────────────────────────────

async def cache_get(key: str) -> Any | None:
    """Fetch a cached value.

    Args:
        key: Cache key.

    Returns:
        The stored value, or ``None`` on a miss. Any backend error is counted
        and treated as a miss — this function never raises.
    """
    if not key:
        return None
    if await cache_backend_async() == "redis":
        try:
            blob = await _redis.get(key)
            if blob is None:
                _record_miss()
                return None
            _record_hit()
            return _decode(blob)
        except Exception as exc:  # noqa: BLE001
            with _stats_lock:
                _stats["errors"] += 1
            log.warning("cache get failed (%s) — serving miss", type(exc).__name__)
            _demote_to_memory()
            found, value = _memory.get(key)
            if not found:
                _record_miss()
                return None
            _record_hit()
            return value

    found, value = _memory.get(key)
    if not found:
        _record_miss()
        return None
    _record_hit()
    return value


async def cache_set(key: str, value: Any, ttl: float | None = None) -> None:
    """Store a value with a TTL.

    Args:
        key: Cache key.
        value: Any JSON-serialisable value.
        ttl: Lifetime in seconds; ``None`` falls back to
            ``config.CACHE_TTL_SECONDS``. A TTL ≤ 0 means "never expires".

    Raises:
        Nothing — encoding and backend failures are logged and swallowed.
    """
    if not key:
        return
    effective_ttl = getattr(config, "CACHE_TTL_SECONDS", 300.0) if ttl is None else float(ttl)
    blob = _encode(value)
    if blob is None:
        return

    with _stats_lock:
        _stats["sets"] += 1
    get_metrics().incr("cache.set")

    if await cache_backend_async() == "redis":
        try:
            if effective_ttl and effective_ttl > 0:
                await _redis.set(key, blob, ex=int(effective_ttl))
            else:
                await _redis.set(key, blob)
            return
        except Exception as exc:  # noqa: BLE001
            with _stats_lock:
                _stats["errors"] += 1
            log.warning("cache set failed (%s) — value not stored", type(exc).__name__)
            _demote_to_memory()
            _memory.set(key, value, effective_ttl)
            return

    _memory.set(key, value, effective_ttl)


async def cache_delete(key: str) -> bool:
    """Delete one key.

    Args:
        key: Cache key.

    Returns:
        ``True`` when a value was removed. Never raises.
    """
    if not key:
        return False
    if await cache_backend_async() == "redis":
        try:
            removed = await _redis.delete(key)
        except Exception as exc:  # noqa: BLE001
            with _stats_lock:
                _stats["errors"] += 1
            log.warning("cache delete failed (%s)", type(exc).__name__)
            return False
    else:
        removed = _memory.delete(key)
    with _stats_lock:
        _stats["deletes"] += int(bool(removed))
    return bool(removed)


async def cache_delete_prefix(prefix: str) -> int:
    """Delete every key sharing a prefix (namespace invalidation).

    Args:
        prefix: Key prefix, e.g. ``"case:"``.

    Returns:
        Number of keys removed. Never raises.
    """
    if not prefix:
        return 0
    if await cache_backend_async() == "redis":
        removed = 0
        try:
            # SCAN, never KEYS: a blocking KEYS on a large keyspace stalls the
            # Redis event loop for every other client.
            async for key in _redis.scan_iter(match=f"{prefix}*", count=500):
                if key is None:
                    continue
                removed += await _redis.delete(key)
        except Exception as exc:  # noqa: BLE001
            with _stats_lock:
                _stats["errors"] += 1
            log.warning("cache prefix delete failed (%s)", type(exc).__name__)
            return removed
        with _stats_lock:
            _stats["deletes"] += removed
        return removed
    return _memory.delete_prefix(prefix)


def cache_stats() -> dict:
    """Return hit/miss counters plus the active backend and size.

    Returns:
        ``{"backend", "hits", "misses", "sets", "deletes", "errors", "size",
        "hit_rate", "max_size"}``. ``size`` is the entry count for the
        in-memory backend, or ``None`` when Redis owns the keyspace.
    """
    with _stats_lock:
        hits, misses = _stats["hits"], _stats["misses"]
        errors = _stats["errors"]
        sets, deletes = _stats["sets"], _stats["deletes"]
    total = hits + misses
    backend = cache_backend()
    return {
        "backend": backend,
        "hits": hits,
        "misses": misses,
        "sets": sets,
        "deletes": deletes,
        "errors": errors,
        "size": _memory.size() if backend == "memory" else None,
        "max_size": getattr(config, "CACHE_MAX_SIZE", 1000),
        "hit_rate": round(hits / total, 3) if total else 0.0,
    }


def reset_stats() -> None:
    """Zero the hit/miss counters and clear memory. Intended for tests."""
    global _stats
    with _stats_lock:
        _stats = {"hits": 0, "misses": 0, "sets": 0, "deletes": 0, "errors": 0, "key_hits": {}}
    _memory.clear()


# ── Decorator ────────────────────────────────────────────────────────────────

def _render_key(template: str, args: tuple, kwargs: dict) -> str:
    """Build a cache key from a template and the call's arguments.

    Supports ``str.format`` placeholders plus a stable ``{__args__}`` digest
    for the un-shapeable cases (unhashable / repr-unstable values).

    Args:
        template: Key template, e.g. ``"shodan:ip:{ip}"``.
        args: Positional arguments from the call.
        kwargs: Keyword arguments from the call.

    Returns:
        A bounded, deterministic cache key.
    """
    try:
        return template.format(*args, **kwargs)[:512]
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        # No placeholder matches this call's signature (e.g. the template
        # names a parameter the function doesn't have). Fall back to a digest
        # of everything, so the key is still stable and collision-safe.
        try:
            payload = json.dumps(
                {"args": [_safe_repr(a) for a in args], "kwargs": kwargs},
                sort_keys=True, default=str,
            )
        except (TypeError, ValueError):
            payload = repr(args) + repr(sorted(kwargs.items()))
        digest = hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:32]
        return f"{template}:{digest}"


def _safe_repr(value: Any) -> Any:
    """Coerce a call argument to something JSON can hold."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return repr(value)


def cached(ttl: float = 300.0, key: str | None = None) -> Callable:
    """Cache an async function's result for ``ttl`` seconds.

    Usable bare (``@cached``) or called (``@cached(ttl=300, key="x:{v}")``).
    The cache is a pure optimisation: a backend error re-runs the wrapped
    function rather than propagating.

    Args:
        ttl: Default lifetime in seconds, overridable per-``cache_set`` call.
        key: Key template. ``str.format`` placeholders are filled from the
            call's arguments; when no placeholder matches, a SHA-256 digest of
            the arguments is appended. Defaults to
            ``"<module>.<qualname>:<digest>"``.

    Returns:
        A decorator that preserves the wrapped function's metadata and adds
        ``.invalidate(*args, **kwargs)``.
    """
    if callable(ttl) and key is None:  # bare @cached usage
        fn = ttl
        return cached()(fn)

    def decorator(fn: Callable) -> Callable:
        prefix = key or f"{fn.__module__}.{fn.__qualname__}"
        assert asyncio.iscoroutinefunction(fn), (
            f"@cached requires an async function, got {fn.__name__}"
        )

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                cache_key = _render_key(prefix, args, kwargs)
            except Exception:  # noqa: BLE001 — never block the wrapped call
                cache_key = f"{prefix}:{id(args)}"
            try:
                hit = await cache_get(cache_key)
            except Exception:  # noqa: BLE001
                hit = None
                cache_key = None  # type: ignore[assignment]
            if cache_key is not None:
                if hit is not None:
                    get_metrics().incr("cache.decorator_hit", fn=fn.__qualname__)
                    return hit
                get_metrics().incr("cache.decorator_miss", fn=fn.__qualname__)

            result = await fn(*args, **kwargs)
            if cache_key is not None:
                try:
                    await cache_set(cache_key, result, ttl)
                except Exception:  # noqa: BLE001
                    pass
            return result

        async def invalidate(*args: Any, **kwargs: Any) -> bool:
            """Drop this function's cached value for one call signature.

            Args:
                *args: Arguments matching the original call.
                **kwargs: Keyword arguments matching the original call.

            Returns:
                ``True`` when an entry was removed.
            """
            try:
                return await cache_delete(_render_key(prefix, args, kwargs))
            except Exception:  # noqa: BLE001
                return False

        wrapper.invalidate = invalidate  # type: ignore[attr-defined]
        wrapper.cache_prefix = prefix  # type: ignore[attr-defined]
        return wrapper

    return decorator
