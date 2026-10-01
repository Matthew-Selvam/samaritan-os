"""
celery_app.py — Celery Application Factory
=============================================
Wires the ``signal_os`` Celery app to Redis and provides the task registry that
``backend/tasks.py`` decorates.

**Design rule: this module must import cleanly even when Celery is not
installed or Redis is down.** Nothing here connects to a broker at import time —
Celery only dials the broker lazily, when a task is published or a worker
starts. So a missing dependency is detected once, recorded in
:data:`CELERY_IMPORT_ERROR` / :data:`CELERY_AVAILABLE`, and the process falls
back to the in-process path in ``tasks.py``. The platform is required to work
with zero infrastructure; this module is what makes that guarantee hold.

Settings, and why each one is here:

``task_serializer="json"``
    Payloads are JSON only. Celery's default (pickle) would let a broker
    message execute arbitrary code in the worker — never enable it on a broker
    you do not fully control.
``result_serializer="json"``
    Same reasoning for results; also keeps ``AsyncResult.result`` usable from
    any language.
``accept_content=["json"]``
    A worker must refuse non-JSON content rather than trust the producer.
``task_acks_late=True``
    The broker acknowledges a task only *after* the worker finishes. A worker
    killed mid-investigation returns the message to the queue instead of
    silently dropping the job. Combined with ``reject_on_worker_lost`` this is
    what makes at-least-once delivery safe.
``worker_prefetch_multiplier=1``
    Each worker pulls one message at a time. Investigations are long and
    unevenly timed; the default prefetch of 4 would let one worker hoard the
    queue while others idle.
``task_acks_late`` + ``task_time_limit=600`` / ``task_soft_time_limit=540``
    A hard kill at 600s guarantees a stuck investigation cannot pin a worker
    forever; the soft limit fires 60s earlier so task code can clean up and
    record a structured failure. The 60s gap is deliberate — it is the window
    ``SoftTimeLimitExceeded`` handlers need to mark the investigation failed in
    the store before the hard kill lands.
``timezone="UTC"``, ``enable_utc=True``
    Timestamps in schedules and logs must not shift with the host's locale.
``result_expires``
    Results are only useful while a client is polling; one day matches the
    investigation-retention window.

Usage::

    # run a worker (from backend/, so `tasks` is importable)
    celery -A celery_app.celery_app worker --loglevel=info

    # or point at the factory instead of the instance
    celery -A celery_app:make_celery worker --loglevel=info
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

import config

__all__ = [
    "celery_app",
    "CELERY_AVAILABLE",
    "CELERY_IMPORT_ERROR",
    "make_celery",
    "broker_reachable",
    "celery_status",
    "TASK_NAMES",
]

#: Broker/backend DSN. `CELERY_BROKER_URL` wins over `REDIS_URL` (config already
#: applies that fallback), and an explicit empty value disables the queue.
BROKER_URL = (getattr(config, "CELERY_BROKER_URL", "") or "").strip()

#: Human-readable reason Celery is unusable. ``None`` when it imported fine.
CELERY_IMPORT_ERROR: str | None = None
#: The Celery application, or ``None`` when Celery could not be imported.
celery_app: Any = None

#: Task names published in CONTRACTS.md §9. Declared here so the registry is
#: inspectable without importing celery, and so ops endpoints can diff the
#: implemented names against what clients expect.
TASK_NAMES: tuple[str, ...] = (
    "signal_os.investigate",
    "signal_os.sentinel_scan",
    "signal_os.report",
)


def make_celery(name: str = "signal_os", broker: str | None = None,
                backend: str | None = None) -> Any:
    """Construct a fully configured Celery application.

    Kept separate from the module-level instance so tests and alternative
    deployments can build an isolated app (a different broker, a different
    name) without mutating the process-wide one. Importing Celery happens
    inside this function: a missing dependency is reported by raising
    :class:`ImportError` to the caller, and the module-level instance below
    catches it so *importing this module never fails*.

    Args:
        name: Celery application name (also the queue namespace).
        broker: Broker DSN. Defaults to :data:`BROKER_URL`.
        backend: Result-backend DSN. Defaults to the broker.

    Returns:
        A configured :class:`celery.Celery` instance.

    Raises:
        ImportError: When the ``celery`` package is not installed.
    """
    # Lazy import: celery is a heavy optional dependency and must not be a
    # module-scope import in a codebase that has to boot without it.
    from celery import Celery  # noqa: PLC0415 — intentionally lazy

    dsn = (broker if broker is not None else BROKER_URL) or "memory://"
    app = Celery(
        name,
        broker=dsn,
        backend=(backend if backend is not None else dsn),
        include=["tasks"],  # the module holding the @celery_app.task wrappers
    )
    app.conf.update(
        # ── serialization: JSON only, never pickle ──────────────────────────
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
        # ── delivery: at-least-once, no prefetch hoarding ──────────────────
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        # ── timeouts: soft first (cleanup), then hard kill ───────────────────
        task_time_limit=600,
        task_soft_time_limit=540,
        # ── results ─────────────────────────────────────────────────────────
        result_expires=int(os.getenv("CELERY_RESULT_EXPIRES", str(86400))),
        result_backend_transport_options={},
        # ── scheduling / logs ───────────────────────────────────────────────
        timezone="UTC",
        enable_utc=True,
        worker_hijack_root_logger=False,
        broker_connection_retry_on_startup=True,
    )
    return app


def _try_build_app() -> Any:
    """Build the process-wide Celery app, or return ``None`` if impossible.

    Returns:
        A configured Celery app, or ``None`` with :data:`CELERY_IMPORT_ERROR`
        set explaining why (celery missing, or no broker configured).
    """
    global CELERY_IMPORT_ERROR

    if not getattr(config, "CELERY_ENABLED", True):
        CELERY_IMPORT_ERROR = "CELERY_ENABLED=false"
        return None
    if not BROKER_URL:
        CELERY_IMPORT_ERROR = "no broker URL configured"
        return None
    try:
        return make_celery()
    except ImportError as exc:
        CELERY_IMPORT_ERROR = f"celery not installed ({exc})"
        return None
    except Exception as exc:  # noqa: BLE001 — a broken celery install is not fatal
        CELERY_IMPORT_ERROR = f"celery init failed ({type(exc).__name__}: {exc})"
        return None


celery_app = _try_build_app()

#: True when a Celery app exists and the broker is usable.
CELERY_AVAILABLE: bool = celery_app is not None

# ── Broker probe ─────────────────────────────────────────────────────────────

#: Cached reachability of the broker. ``None`` means "not probed yet".
_broker_ok: bool | None = None
_probe_lock = threading.Lock()
#: Timestamp (monotonic) of the last probe.
_probe_at: float = 0.0
#: How long a probe result stays fresh (seconds).
PROBE_TTL: float = 10.0
#: Seconds to wait for a broker probe before treating Redis as unavailable.
PROBE_TIMEOUT = float(os.getenv("BROKER_PROBE_TIMEOUT", "1.5"))


def broker_reachable(timeout: float | None = None, *,
                     use_cache: bool = True) -> bool:
    """Check whether the Celery broker answers.

    A TCP-level ping via the broker URL. Used to decide, per call, whether to
    publish a task or run it in-process — a broker that is up at boot can still
    be down at request time, and silently dropping a task onto a dead queue is
    the failure mode this avoids.

    Args:
        timeout: Socket timeout in seconds. Defaults to :data:`PROBE_TIMEOUT`.
        use_cache: Reuse the last probe result for :data:`PROBE_TTL` seconds. A
            per-request probe against a dead host would otherwise add the
            timeout to every response.

    Returns:
        ``True`` when the broker accepted a connection. Never raises — an
        unreachable broker is a normal condition, not an error.
    """
    global _broker_ok, _probe_at

    if celery_app is None:
        return False

    now = time.monotonic()
    with _probe_lock:
        if (
            use_cache
            and _broker_ok is not None
            and (now - _probe_at) < PROBE_TTL
        ):
            return _broker_ok

    result = _raw_broker_probe(timeout)

    with _probe_lock:
        _broker_ok = result
        _probe_at = time.monotonic()
    return result


def _raw_broker_probe(timeout: float | None = None) -> bool:
    """Open one connection to the broker and immediately close it."""
    wait = PROBE_TIMEOUT if timeout is None else float(timeout)
    try:
        if BROKER_URL.startswith("redis://") or BROKER_URL.startswith("rediss://"):
            # Talk Redis directly: one PING over a short-lived socket. Cheaper
            # and more predictable than spinning up a Celery connection pool
            # just to answer "is the broker up?".
            import redis  # noqa: PLC0415 — lazy, optional dependency

            client = redis.Redis.from_url(
                BROKER_URL,
                socket_connect_timeout=wait,
                socket_timeout=wait,
            )
            try:
                return bool(client.ping())
            finally:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
        # Non-Redis broker (AMQP, filesystem, …): let kombu decide.
        connection = celery_app.connection()
        try:
            connection.ensure_connection(max_retries=0, timeout=wait)
            return True
        finally:
            try:
                connection.release()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 — unreachable broker is a normal state
        return False


def celery_status() -> dict:
    """Summarise Celery readiness for the ops endpoints.

    Returns:
        ``{"available", "enabled", "broker_url", "broker_reachable",
        "error", "tasks"}``. Secret-bearing DSNs (a password in the URL) are
        replaced with ``***`` before the value leaves this function.
    """
    reachable = False
    if celery_app is not None:
        reachable = broker_reachable()
    return {
        "available": CELERY_AVAILABLE,
        "enabled": bool(getattr(config, "CELERY_ENABLED", True)),
        "broker_url": _mask_dsn(BROKER_URL),
        "broker_reachable": reachable,
        "error": CELERY_IMPORT_ERROR,
        "tasks": list(TASK_NAMES),
    }


def _mask_dsn(dsn: str) -> str:
    """Strip any password from a broker DSN.

    Args:
        dsn: e.g. ``redis://user:hunter2@localhost:6379/0``.

    Returns:
        The DSN with the password replaced by ``***``.
    """
    if not dsn or "@" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    if not rest:
        return dsn
    credentials, _, host = rest.rpartition("@")
    if not credentials or ":" not in credentials:
        return dsn
    user, _, _password = credentials.partition(":")
    return f"{scheme}://{user}:***@{host}"