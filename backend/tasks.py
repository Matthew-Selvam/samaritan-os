"""
tasks.py — Investigation Execution Layer
==========================================
The single entry point for running an investigation, in three interchangeable
modes:

1. **Local (default, always available)** —
   :func:`run_investigation_local` runs the APEX pipeline in the current event
   loop and returns a plain JSON-serialisable dict. This is the path the whole
   platform depends on: no Redis, no Celery worker, no Postgres.
2. **Celery** — :func:`run_investigation` publishes to the queue when
   ``CELERY_ENABLED`` and the broker answers; the ``@celery_app.task`` wrappers
   below then execute on a worker.
3. **Direct task call** — the wrappers are also plain callables, so
   ``run_investigation.apply(args=[payload])`` or ``run_investigation(payload)``
   works in-process for tests and for the sync endpoint.

Why the local path exists at all: CONTRACTS.md §9 requires that "the app must
work with zero infrastructure". A single-module Celery import at module scope
would break the serverless/Vercel deployment where no broker exists, so every
heavy import here is lazy and every failure mode degrades to local execution.

**Serialization contract**: task payloads are *request parameters only* —
``{"input": str, "input_type": str | None, "case_id": str | None, ...}``. An
:class:`~agents.base.AgentResult` never crosses a queue boundary; only
:func:`_agent_result_payload` dicts do. That is what keeps ``task_serializer``
on JSON and avoids both pickling arbitrary objects and leaking raw agent state
into Redis.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable

import celery_app as celery_module
import config
from celery_app import broker_reachable
from observability import get_logger, get_metrics, redact

__all__ = [
    "run_investigation",
    "run_investigation_local",
    "sentinel_scan",
    "sentinel_scan_local",
    "build_report",
    "build_report_local",
    "celery_available",
    "celery_tasks",
    "task_status",
]

log = get_logger("signal-os.tasks")

#: Task names, mirrored from celery_app so callers need not import it.
TASK_INVESTIGATE = "signal_os.investigate"
TASK_SENTINEL = "signal_os.sentinel_scan"
TASK_REPORT = "signal_os.report"

#: Statuses an investigation record may carry.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"


# ── Concurrency guard ────────────────────────────────────────────────────────

#: Bounds how many investigations run at once in a single process. Without it a
#: burst of submissions would launch unbounded agent swarms on one event loop,
#: and the slowest agent of each would decide everyone's latency.
_concurrency: asyncio.Semaphore | None = None
#: Counters for the ops endpoints.
_inflight: dict[str, int] = {"active": 0, "queued": 0, "completed": 0, "failed": 0}


def _semaphore() -> asyncio.Semaphore:
    """Return this process's investigation semaphore, creating it if needed.

    The semaphore is created lazily because :class:`asyncio.Semaphore` binds to
    the running loop on first use; building it at import time would break under
    a different loop (pytest-asyncio, Celery prefork workers, uvicorn reload).
    """
    global _concurrency
    if _concurrency is None:
        limit = max(1, int(getattr(config, "MAX_CONCURRENT_INVESTIGATIONS", 4)))
        _concurrency = asyncio.Semaphore(limit)
    return _concurrency


# ── Serialization helpers ────────────────────────────────────────────────────

def _json_safe(value: Any, _depth: int = 0) -> Any:
    """Coerce arbitrary agent output into a JSON-serialisable value.

    Agent ``output`` payloads are connector-dependent and occasionally contain
    sets, datetimes, or ``bytes``. Celery's JSON serializer would reject the
    whole result on any one of those, so the result dies — and the investigation
    looks like a failure when it actually succeeded. Coercing here means a
    successful run always round-trips through the queue.

    Args:
        value: Any value from an agent result.
        _depth: Recursion guard.

    Returns:
        A structure made only of dict/list/tuple/str/int/float/bool/None.
    """
    if _depth > 12:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): _json_safe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v, _depth + 1) for v in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _json_safe(value.to_dict(), _depth + 1)
        except Exception:  # noqa: BLE001 — fall through to repr
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def _agent_result_payload(result: Any, *, steps: list[str] | None = None,
                          role: str = "", icon: str = "") -> dict:
    """Flatten one :class:`AgentResult` into a transport-safe dict.

    The full ``output`` blob is deliberately *not* copied here — agent outputs
    can be megabytes (raw page dumps, breach records). The summary fields plus
    entity/signal counts are what the task result needs; the heavy payload is
    already persisted in the investigation record.

    Args:
        result: An ``AgentResult`` or an already-serialized dict.
        steps: Live step lines captured from the agent, when available.
        role: Agent role, when the caller knows it.
        icon: Agent icon, when the caller knows it.

    Returns:
        A JSON-serialisable dict describing this agent's contribution.
    """
    if isinstance(result, dict):
        data = result
        agent = str(data.get("agent") or "")
        status = str(data.get("status") or "unknown")
        confidence = _float_or_zero(data.get("confidence"))
        latency = _float_or_zero(data.get("latency_s"))
        error = data.get("error")
        output = data.get("output")
        entities = data.get("entities_found") or []
        signals = data.get("signals") or []
        reasoning = str(data.get("reasoning") or "")
        resolved_role = str(data.get("role") or role)
        resolved_icon = str(data.get("icon") or icon)
        resolved_steps = list(steps if steps is not None else (data.get("steps") or []))
    else:
        agent = str(getattr(result, "agent", "") or "")
        status = str(getattr(result, "status", "unknown") or "unknown")
        confidence = _float_or_zero(getattr(result, "confidence", 0.0))
        latency = _float_or_zero(getattr(result, "latency_s", 0.0))
        error = getattr(result, "error", None)
        output = getattr(result, "output", None)
        entities = getattr(result, "entities_found", None) or []
        signals = getattr(result, "signals", None) or []
        reasoning = str(getattr(result, "reasoning", "") or "")
        resolved_role = role
        resolved_icon = icon
        resolved_steps = list(steps or [])

    return {
        "agent": agent,
        "role": resolved_role,
        "icon": resolved_icon,
        "status": status,
        "confidence": confidence,
        "reasoning": reasoning[:500],
        "latency_s": latency,
        "entities_found": len(entities) if hasattr(entities, "__len__") else 0,
        "signals": len(signals) if hasattr(signals, "__len__") else 0,
        "steps": resolved_steps[-25:],
        "error": (str(error)[:300] if error else None),
        # Only the compact parts of `output` travel with the task result; the
        # rest stays in the persisted investigation record.
        "output_summary": _output_summary(output),
    }


def _output_summary(output: Any) -> dict:
    """Summarise an agent's ``output`` into counts and small scalar fields.

    Args:
        output: Whatever an agent put in ``AgentResult.output``.

    Returns:
        A small dict of counts plus any short string/bool keys. Large lists and
        blobs are reduced to their length.
    """
    if output is None:
        return {}
    if not isinstance(output, dict):
        return {"preview": str(output)[:200]}
    summary: dict[str, Any] = {}
    for key, value in output.items():
        if isinstance(value, bool) or isinstance(value, (int, float)):
            summary[str(key)] = value
        elif isinstance(value, str):
            if len(value) <= 300:
                summary[str(key)] = value
        elif hasattr(value, "__len__"):
            summary[f"{key}_count"] = len(value)
    return summary


def _float_or_zero(value: Any) -> float:
    """Coerce to float, returning 0.0 for anything unconvertible."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if result != result or result in (float("inf"), float("-inf")):
        return 0.0
    return result


def _new_ids(payload: dict) -> tuple[str, str]:
    """Resolve ``(inv_id, case_id)`` for a run.

    Args:
        payload: Request params; may already carry ids from a submit endpoint.

    Returns:
        The pair of 8-char hex ids to use for this run.
    """
    inv_id = str(payload.get("inv_id") or uuid.uuid4().hex[:8])
    case_id = str(payload.get("case_id") or uuid.uuid4().hex[:8])
    return inv_id, case_id


async def _persist(payload: dict, inv_id: str, case_id: str, patch: dict) -> None:
    """Write one investigation patch to the store, best-effort.

    Persistence must never be able to fail an investigation: the store itself
    degrades to memory when its backend is down, and any residual error is
    logged and swallowed so the caller still gets a result.

    Args:
        payload: Original request params (for the audit trail).
        inv_id: Investigation id.
        case_id: Case id.
        patch: Fields to merge into the record.
    """
    try:
        from store import get_store  # lazy: keeps the import graph acyclic

        store = await get_store()
        existing = await store.get_investigation(inv_id)
        if existing is None:
            record = {
                "inv_id": inv_id,
                "case_id": case_id,
                "input": str(payload.get("input") or "")[:500],
                "input_type": payload.get("input_type"),
                "status": STATUS_QUEUED,
            }
            record.update(patch)
            await store.save_investigation(record)
        else:
            await store.update_investigation(inv_id, patch)
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort
        log.warning(
            "store write failed for %s (%s) — continuing without persistence",
            inv_id, type(exc).__name__,
        )


# ── Core pipeline ────────────────────────────────────────────────────────────

def _build_report(result: Any, agent_results: list[dict]) -> dict:
    """Shape an APEX result into the report dict the frontend expects.

    Mirrors ``main._build_report`` so both the legacy inline pipeline and this
    task layer emit the identical contract (CONTRACTS.md §11 ``Report``).

    Args:
        result: The APEX ``AgentResult``.
        agent_results: The serialized per-agent dicts from that result.

    Returns:
        The report payload.
    """
    out = getattr(result, "output", None) or {}
    if not isinstance(out, dict):
        out = {}
    confidence = _float_or_zero(getattr(result, "confidence", 0.0))
    return {
        "summary": (
            f"Investigation complete — {out.get('input_type', 'unknown')}; "
            f"{len(agent_results)} agents, confidence {confidence:.0%}"
        ),
        "input_type": out.get("input_type"),
        "agents_activated": out.get("agents_activated", []),
        "entities": _json_safe(out.get("entities", [])),
        "signals": _json_safe(out.get("signals", [])),
        "graph": _json_safe(out.get("graph", {"nodes": [], "edges": []})),
        "timeline": _json_safe(out.get("timeline", [])),
        "markdown": out.get("report_markdown"),
        "confidence": confidence,
        "latency_s": _float_or_zero(getattr(result, "latency_s", 0.0)),
    }


async def run_investigation_local(payload: dict) -> dict:
    """Run the full investigation pipeline in-process.

    **This is the path the whole platform depends on.** It needs nothing but
    the local filesystem: no broker, no worker, no database. Any agent that
    fails is isolated by APEX into an ``error`` result, so this returns a
    populated result even when every connector and every LLM provider is
    unavailable.

    Args:
        payload: Request parameters. Recognised keys:

            * ``input`` (str) — the investigation target. **Required.**
            * ``input_type`` (str | None) — override APEX's detection.
            * ``case_id`` (str | None) — attach to an existing case; a new id
              is minted when absent.
            * ``inv_id`` (str | None) — reuse a caller-allocated id.
            * ``emit`` (callable) — optional async step sink for WS streaming.

    Returns:
        A JSON-serialisable dict with ``status``, ``inv_id``, ``case_id``,
        ``input``, ``input_type``, ``agent_results`` (slimmed),
        ``report``, ``confidence``, ``latency_s``, ``steps``, and ``error``
        (only when the run failed). Never raises.
    """
    started = time.monotonic()
    params = dict(payload or {})
    raw = str(params.get("input") or params.get("query") or "").strip()
    inv_id, case_id = _new_ids(params)
    metrics = get_metrics()

    if not raw:
        result = {
            "status": STATUS_ERROR,
            "inv_id": inv_id,
            "case_id": case_id,
            "input": "",
            "input_type": params.get("input_type"),
            "agent_results": [],
            "report": None,
            "confidence": 0.0,
            "latency_s": 0.0,
            "steps": [],
            "error": "input is required",
        }
        metrics.incr("investigation.failed", reason="empty_input")
        return result

    steps: list[str] = []
    emit: Callable[[str], Awaitable[None]] | None = params.get("emit")

    async def _emit(message: str) -> None:
        """Record a step locally and forward it to the caller's sink."""
        steps.append(message)
        get_metrics().incr("investigation.steps")
        if emit is not None:
            try:
                await emit(message)
            except Exception:  # noqa: BLE001 — a dead WS client must not fail the run
                pass

    await _persist(params, inv_id, case_id, {"status": STATUS_RUNNING})
    _inflight["active"] += 1
    metrics.incr("investigation.started")

    context: dict[str, Any] = {
        "emit": _emit,
        "input_type": params.get("input_type"),
        "case_id": case_id,
    }
    # Pass through any extra context keys an API layer attached (lang, deep…).
    for passthrough in ("lang", "deep"):
        if passthrough in params:
            context[passthrough] = params[passthrough]

    try:
        async with _semaphore():
            apex = _load_apex()()
            agent_result = await apex.run(raw, context=context)
    except Exception as exc:  # noqa: BLE001 — a pipeline failure is a result, not a crash
        latency = round(time.monotonic() - started, 3)
        _inflight["active"] = max(0, _inflight["active"] - 1)
        _inflight["failed"] += 1
        log.error("investigation %s failed: %s", inv_id, type(exc).__name__)
        metrics.incr("investigation.failed", reason=type(exc).__name__)
        await _persist(params, inv_id, case_id, {
            "status": STATUS_ERROR,
            "error": str(exc)[:500],
            "ended_at": _now_iso(),
            "latency_s": latency,
            "steps": steps,
        })
        return {
            "status": STATUS_ERROR,
            "inv_id": inv_id,
            "case_id": case_id,
            "input": raw[:500],
            "input_type": params.get("input_type"),
            "agent_results": [],
            "report": None,
            "confidence": 0.0,
            "latency_s": latency,
            "steps": steps,
            "error": str(exc)[:500],
        }

    _inflight["active"] = max(0, _inflight["active"] - 1)
    _inflight["completed"] += 1

    out = getattr(agent_result, "output", None) or {}
    raw_agent_results = out.get("agent_results", []) if isinstance(out, dict) else []
    slimmed = [
        _agent_result_payload(item)
        for item in (raw_agent_results or [])
    ]
    report = _build_report(agent_result, slimmed)
    latency = _float_or_zero(getattr(agent_result, "latency_s", 0.0)) or round(
        time.monotonic() - started, 3
    )
    status = (
        STATUS_DONE
        if str(getattr(agent_result, "status", "")) != "error"
        else STATUS_ERROR
    )

    metrics.incr("investigation.completed", status=status)
    metrics.timing("investigation.latency_s", latency)
    metrics.gauge("investigations.inflight", _inflight["active"])

    await _persist(params, inv_id, case_id, {
        "status": status,
        "input_type": out.get("input_type") if isinstance(out, dict) else None,
        "agents": _json_safe(slimmed),
        "confidence": _float_or_zero(getattr(agent_result, "confidence", 0.0)),
        "latency_s": latency,
        "report": _json_safe(report),
        "steps": steps,
        "ended_at": _now_iso(),
    })
    # Register the case so /api/cases/{id} resolves for this investigation.
    await _ensure_case_async(case_id, raw)

    return {
        "status": status,
        "inv_id": inv_id,
        "case_id": case_id,
        "input": raw[:500],
        "input_type": out.get("input_type") if isinstance(out, dict) else None,
        "agents_activated": _json_safe(
            out.get("agents_activated", []) if isinstance(out, dict) else []
        ),
        "agent_results": slimmed,
        "report": report,
        "confidence": _float_or_zero(getattr(agent_result, "confidence", 0.0)),
        "latency_s": latency,
        "steps": steps,
    }


def _load_apex() -> Any:
    """Import ``ApexAgent`` lazily and return the class.

    Lazy so importing ``tasks`` never pulls the whole agent registry (and every
    connector dependency) into a worker process that only needs the queue.

    Returns:
        The :class:`~agents.apex.ApexAgent` class.

    Raises:
        ImportError: When the agent package is unavailable.
    """
    from agents.apex import ApexAgent  # noqa: PLC0415 — intentionally lazy

    return ApexAgent


def _ensure_case(case_id: str, target: str) -> None:
    """Register a case for this run if it does not exist yet.

    Fire-and-forget on purpose: a case row is a convenience for
    ``GET /api/cases/{id}``, never a reason to fail or delay an investigation.
    Scheduled on the running loop so the caller is not blocked by a store write.

    Args:
        case_id: Case id to upsert.
        target: Investigation input, used as the case's default target.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("no running loop — skipping case registration for %s", case_id)
        return
    loop.create_task(_ensure_case_async(case_id, target))


async def _ensure_case_async(case_id: str, target: str) -> None:
    """Upsert a case row, swallowing every failure.

    Args:
        case_id: Case id to create when missing.
        target: Investigation input, used as the case's default target.
    """
    try:
        from store import get_store  # lazy

        store = await get_store()
        existing = await store.get_case(case_id)
        if existing is None:
            await store.upsert_case({
                "case_id": case_id,
                "name": str(target or case_id)[:120],
                "target": str(target or "")[:200],
            })
    except Exception as exc:  # noqa: BLE001 — case registration is a convenience
        log.warning("case upsert failed for %s (%s)", case_id, type(exc).__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ── Sentinel scan ────────────────────────────────────────────────────────────

async def sentinel_scan_local(case_id: str, target: str) -> dict:
    """Register or diff a SENTINEL monitoring snapshot, in-process.

    Args:
        case_id: Case the target belongs to.
        target: The monitored subject (username, domain, …).

    Returns:
        ``{"status", "case_id", "target", "output", "latency_s", "error"}``.
        Never raises.
    """
    started = time.monotonic()
    try:
        from agents.sentinel import SentinelAgent  # lazy

        agent = SentinelAgent()
        agent_result = await agent.run(
            str(target),
            context={"case_id": case_id},
        )
    except Exception as exc:  # noqa: BLE001
        log.error("sentinel scan failed for %s (%s)", case_id, type(exc).__name__)
        return {
            "status": STATUS_ERROR,
            "case_id": case_id,
            "target": target,
            "output": None,
            "latency_s": round(time.monotonic() - started, 3),
            "error": str(exc)[:300],
        }

    get_metrics().incr("sentinel.scan")
    return {
        "status": STATUS_DONE,
        "case_id": case_id,
        "target": target,
        "agent": "SENTINEL",
        "output": _json_safe(getattr(agent_result, "output", None)),
        "confidence": _float_or_zero(getattr(agent_result, "confidence", 0.0)),
        "latency_s": _float_or_zero(getattr(agent_result, "latency_s", 0.0)),
        "error": getattr(agent_result, "error", None),
    }


# ── Report builder ───────────────────────────────────────────────────────────

async def build_report_local(inv_id: str, fmt: str = "markdown") -> dict:
    """Return (and persist) the report for a stored investigation.

    Args:
        inv_id: Investigation whose report is wanted.
        fmt: ``markdown`` (default) or ``json``.

    Returns:
        ``{"status", "inv_id", "format", "report", "markdown", "report_id",
        "error"}``. Never raises.
    """
    try:
        from store import get_store  # lazy

        store = await get_store()
        record = await store.get_investigation(inv_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": STATUS_ERROR,
            "inv_id": inv_id,
            "format": fmt,
            "report": None,
            "markdown": None,
            "error": str(exc)[:300],
        }

    if not record:
        return {
            "status": STATUS_ERROR,
            "inv_id": inv_id,
            "format": fmt,
            "report": None,
            "markdown": None,
            "error": "investigation not found",
        }

    report = record.get("report") or {}
    markdown = report.get("markdown") if isinstance(report, dict) else None

    # Persist under the reports table so the reports API can list/export it.
    report_id = None
    try:
        report_id = await store.save_report(
            inv_id,
            str(markdown or ""),
            {"format": fmt, "case_id": record.get("case_id")},
        )
    except Exception as exc:  # noqa: BLE001 — report persistence is best-effort
        log.warning("save_report failed for %s (%s)", inv_id, type(exc).__name__)

    return {
        "status": STATUS_DONE,
        "inv_id": inv_id,
        "format": fmt,
        "report": _json_safe(report),
        "markdown": markdown,
        "report_id": report_id,
        "error": None,
    }


# ── Celery task wrappers ─────────────────────────────────────────────────────

def _run_async(coro: Awaitable[Any]) -> Any:
    """Drive a coroutine to completion from synchronous task code.

    Celery tasks are sync functions. Each task body awaits an async function, so
    it needs an event loop. ``asyncio.run`` gives it a fresh, isolated loop,
    which is what makes the task safe under Celery's prefork worker model (where
    a loop from a previous task may already be closed) and under an
    already-running loop in the caller (handled by the fallback below).

    Args:
        coro: The coroutine to run.

    Returns:
        The coroutine's result.

    Raises:
        Whatever the coroutine raises.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]

    # Already inside a loop (e.g. called from an async endpoint): run the
    # coroutine on a dedicated loop in a worker thread so the running loop is
    # not re-entered — `asyncio.run` raises for that, and nesting loops deadlocks.
    import threading

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro)  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True, name="signal-os-task")
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _payload_slim(payload: dict) -> dict:
    """Reduce a task payload to the request parameters that may be sent.

    Drops anything non-serialisable (a live ``emit`` callback, a file handle)
    and masks secrets, so a credential typed into a form field never reaches
    Redis.

    Args:
        payload: The caller's params.

    Returns:
        A JSON-safe dict of request parameters.
    """
    clean: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if key == "emit" or not isinstance(key, str):
            continue
        if callable(value):
            continue
        clean[key] = value
    return redact(clean)


def celery_available() -> bool:
    """Report whether Celery can actually carry a task right now.

    Used by the ops endpoints so ``GET /api/health/deep`` can tell an operator
    "Celery is off, running in-process" instead of failing silently.

    Returns:
        ``True`` only when Celery is imported, enabled in config, and the broker
        answered a probe. Never raises.
    """
    try:
        if not getattr(config, "CELERY_ENABLED", True):
            return False
        if celery_module.celery_app is None:
            return False
        return bool(broker_reachable())
    except Exception as exc:  # noqa: BLE001 — a status probe must never raise
        log.debug("celery_available probe failed: %s", type(exc).__name__)
        return False


# The Celery app is imported at module scope here on purpose: `tasks.py` is the
# module the worker loads (celery_app's `include=["tasks"]`), so the decorators
# below must execute at import time. This is still lazy with respect to Celery
# *itself* — if celery_app.celery_app is None, `_task` returns a thin callable
# shim instead of a registered task, and local execution stays available.
celery_app = celery_module.celery_app


def _task(*task_args: Any, **task_kwargs: Any) -> Callable[[Callable], Any]:
    """Register a Celery task, degrading to a direct-callable shim.

    With a working Celery app this is exactly ``celery_app.task(...)``. Without
    one it returns the function unchanged (wrapped so its call signature still
    matches), which keeps ``run_investigation(payload)`` usable in-process and
    keeps this module importable with no Celery installed.

    Args:
        *task_args: Positional args for ``celery_app.task``.
        **task_kwargs: Keyword args for ``celery_app.task``.

    Returns:
        A decorator that registers (or passes through) the task function.
    """
    if celery_app is not None:
        return celery_app.task(*task_args, **task_kwargs)

    log.debug("celery unavailable — tasks registered as direct callables")

    def _passthrough(fn: Callable) -> Callable:
        fn.__signal_os_celery_fallback__ = True  # type: ignore[attr-defined]
        return fn

    return _passthrough


@_task(bind=True, name=TASK_INVESTIGATE, max_retries=2, default_retry_delay=5)
def _investigate_task(self: Any = None, payload: dict | None = None,
                      **extra: Any) -> dict:
    """Celery task: run a full investigation on a worker.

    Serializes **only request parameters** — the pipeline's ``AgentResult``
    objects never cross the queue boundary. Retries up to twice, and only for
    infrastructure failures (see :func:`_should_retry`); a pipeline that ran and
    reported an error is returned as a structured result instead of raised, so a
    retry never re-runs agents that already finished.

    Named ``signal_os.investigate`` per CONTRACTS.md §9. The underscore prefix
    keeps this Celery task from shadowing the module-level async dispatcher
    :func:`run_investigation`, which shares its public name.

    Args:
        self: Celery's bound task instance (unused; present because
            ``bind=True``).
        payload: Request params — ``input``, ``input_type``, ``case_id``.
        **extra: Additional request params merged into ``payload``.

    Returns:
        The same dict as :func:`run_investigation_local`.
    """
    request = {**(payload or {}), **extra}
    slim = _payload_slim(request)
    log.info("investigate task started (inv_id=%s)", slim.get("inv_id") or "auto")
    get_metrics().incr("task.investigate")

    try:
        return _run_async(run_investigation_local(request))
    except Exception as exc:  # noqa: BLE001 — surface, retry only if transient
        if _should_retry(self, exc):
            log.warning("investigate task transient failure: %s", type(exc).__name__)
            raise
        log.error("investigate task failed: %s", type(exc).__name__)
        return {
            "status": STATUS_ERROR,
            "inv_id": slim.get("inv_id"),
            "case_id": slim.get("case_id"),
            "agent_results": [],
            "report": None,
            "confidence": 0.0,
            "latency_s": 0.0,
            "error": str(exc)[:500],
        }


@_task(name=TASK_SENTINEL)
def sentinel_scan(case_id: str, target: str) -> dict:
    """Celery task: register or diff a SENTINEL monitoring snapshot.

    Args:
        case_id: Owning case id.
        target: Monitored subject.

    Returns:
        The same dict as :func:`sentinel_scan_local`.
    """
    get_metrics().incr("task.sentinel_scan")
    return _run_async(sentinel_scan_local(case_id, target))


@_task(name=TASK_REPORT)
def build_report(inv_id: str, fmt: str = "markdown") -> dict:
    """Celery task: build/persist a report for a stored investigation.

    Args:
        inv_id: Investigation id.
        fmt: ``markdown`` or ``json``.

    Returns:
        The same dict as :func:`build_report_local`.
    """
    get_metrics().incr("task.report")
    return _run_async(build_report_local(inv_id, fmt))


def _should_retry(task: Any, exc: BaseException) -> bool:
    """Decide whether a task failure is worth retrying.

    Only *infrastructure* failures are retried — a connection to the broker or
    a result backend that dropped, a worker killed by the hard time limit. A
    pipeline that ran and reported an error has already done its work, so
    retrying it would re-run every agent and duplicate the investigation.

    Matching is by exception *class name* rather than by importing kombu's
    exception tree: kombu is only present when Celery is, and this module must
    work without it.

    Args:
        task: The bound Celery task (may be ``None`` in the no-Celery path).
        exc: The raised exception.

    Returns:
        ``True`` when the caller should invoke ``self.retry(...)``.
    """
    transient_markers = (
        "OperationalError", "ConnectionError", "TimeoutError",
        "SoftTimeLimitExceeded", "HardTimeLimitExceeded", "WorkerLostError",
        "BrokerConnectionError", "IncompleteRead", "ConnectionResetError",
        "LostConnection", "ChannelError", "Retry",
    )
    name = type(exc).__name__
    if not any(marker in name for marker in transient_markers):
        return False
    if task is None:
        return False
    retries = getattr(getattr(task, "request", None), "retries", 0)
    return int(retries or 0) < int(getattr(task, "max_retries", 2) or 2)


#: The registered Celery task objects. ``run_investigation`` is the async
#: *dispatcher* (CONTRACTS.md §9's "have the route prefer celery, fall back to
#: local"), so the underlying task is exposed here instead of under that name.
#: ``tasks.celery_app.tasks["signal_os.investigate"]`` resolves to the same
#: object for a worker.
celery_tasks: dict[str, Any] = {
    TASK_INVESTIGATE: _investigate_task,
    TASK_SENTINEL: sentinel_scan,
    TASK_REPORT: build_report,
}


# ── Dispatch ─────────────────────────────────────────────────────────────────

async def run_investigation(payload: dict) -> dict:
    """Run an investigation, preferring Celery and falling back to local.

    Dispatch order:

    1. ``CELERY_ENABLED=false`` → local.
    2. Broker unreachable → local.
    3. Otherwise publish ``signal_os.investigate`` and return the queued
       envelope (``{"status": "queued", "inv_id", "case_id", "task_id"}``).

    A publish that raises — broker died between the probe and the send — also
    falls back to local, because an investigation that runs slowly beats one
    that never runs at all.

    Args:
        payload: Request params (see :func:`run_investigation_local`).

    Returns:
        Either the queued envelope or the full local result dict.
    """
    request = dict(payload or {})
    metrics = get_metrics()

    if not celery_available():
        metrics.incr("investigation.path", path="local")
        return await run_investigation_local(request)

    inv_id, case_id = _new_ids(request)
    slim = _payload_slim({**request, "inv_id": inv_id, "case_id": case_id})

    try:
        async_result = _investigate_task.apply_async(
            kwargs={"payload": slim},
            task_id=inv_id,
        )
    except Exception as exc:  # noqa: BLE001 — broker died mid-publish
        log.warning(
            "celery publish failed (%s) — running locally", type(exc).__name__,
        )
        metrics.incr("investigation.path", path="local_fallback")
        return await run_investigation_local(request)

    metrics.incr("investigation.path", path="celery")
    log.info("investigation %s queued via celery (%s)", inv_id, case_id)
    await _persist(request, inv_id, case_id, {
        "status": STATUS_QUEUED,
        "celery_task_id": getattr(async_result, "id", inv_id),
    })
    return {
        "status": STATUS_QUEUED,
        "inv_id": inv_id,
        "case_id": case_id,
        "task_id": getattr(async_result, "id", inv_id),
        "transport": "celery",
    }


def task_status() -> dict:
    """Summarise the task layer for the ops endpoints.

    Returns:
        ``{"celery_available", "celery_status", "inflight", "limits"}``.
    """
    return {
        "celery_available": celery_available(),
        "celery_status": celery_module.celery_status(),
        "inflight": dict(_inflight),
        "limits": {
            "max_concurrent_investigations": int(
                getattr(config, "MAX_CONCURRENT_INVESTIGATIONS", 4)
            ),
        },
    }