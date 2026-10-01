"""
runtime.py — Investigation Runtime
=====================================
Single source of truth for the long-lived pieces of an investigation run:
the store handle, the WebSocket broadcast bus, and the pipeline driver that
APEX executes.

This module exists so that `main.py` and the route modules in `backend/api/`
share one implementation instead of each keeping its own in-memory dict — the
original design had a global `INVESTIGATIONS` dict in `main.py` that a
multi-worker or multi-process deployment silently split, so `POST
/api/investigate` followed by `GET /api/investigate/{id}` returned
`{"error": "not found"}` whenever the two requests landed on different
workers.

Every optional dependency (store, cache, tasks, security) is imported lazily
and guarded, so the platform runs correctly with none of them present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

log = logging.getLogger("signal-os.runtime")

# ── WebSocket broadcast bus ───────────────────────────────────────────────────
# inv_id → set of live sockets. A set (not a list) so a double-disconnect
# cannot leave a stale reference that a later broadcast tries to write to.
_WS_CLIENTS: dict[str, set[Any]] = {}
_WS_LOCK = asyncio.Lock()


async def register_ws(inv_id: str, ws: Any) -> None:
    """Attach a WebSocket to an investigation's channel.

    Args:
        inv_id: Investigation identifier.
        ws: The accepted WebSocket connection.
    """
    async with _WS_LOCK:
        _WS_CLIENTS.setdefault(inv_id, set()).add(ws)


async def unregister_ws(inv_id: str, ws: Any) -> None:
    """Detach a WebSocket, dropping the channel when its last client leaves.

    Args:
        inv_id: Investigation identifier.
        ws: The WebSocket connection to remove.
    """
    async with _WS_LOCK:
        channel = _WS_CLIENTS.get(inv_id)
        if channel is None:
            return
        channel.discard(ws)
        if not channel:
            _WS_CLIENTS.pop(inv_id, None)


def ws_client_count(inv_id: str) -> int:
    """Return how many WebSocket clients are attached to an investigation.

    Args:
        inv_id: Investigation identifier.

    Returns:
        The number of live subscribers.
    """
    return len(_WS_CLIENTS.get(inv_id, ()))


async def broadcast(inv_id: str, payload: dict) -> None:
    """Send an event to every WebSocket subscribed to an investigation.

    Dead sockets are pruned on the way through. A client that has gone away
    must never be able to fail a pipeline.

    Args:
        inv_id: Investigation identifier.
        payload: JSON-serialisable event payload.
    """
    async with _WS_LOCK:
        targets = list(_WS_CLIENTS.get(inv_id, ()))
    if not targets:
        return

    dead: list[Any] = []
    blob = None
    for ws in targets:
        try:
            blob = blob if blob is not None else json.dumps(payload, default=str)
            await ws.send_text(blob)
        except Exception:  # noqa: BLE001 — a dropped client is routine
            dead.append(ws)
    for ws in dead:
        await unregister_ws(inv_id, ws)


# ── Store handle ──────────────────────────────────────────────────────────────

_STORE: Any = None
_STORE_READY = False


async def get_store_handle() -> Any:
    """Return the shared store, initialising it on first use.

    Falls back to a minimal in-process implementation when the real store
    module is unavailable or fails to start.

    Returns:
        An object implementing the store protocol from CONTRACTS.md §4.
    """
    global _STORE, _STORE_READY
    if _STORE_READY and _STORE is not None:
        return _STORE
    try:
        from store import get_store  # lazy: optional dependency

        store = await get_store()
        await store.start()
        _STORE = store
    except Exception as exc:  # noqa: BLE001 — never block startup on storage
        log.warning("store unavailable (%s) — using in-process store", exc)
        _STORE = _MemoryStore()
    _STORE_READY = True
    return _STORE


class _MemoryStore:
    """Last-resort in-process investigation store.

    Implements just enough of the store protocol for the API to function when
    the real store cannot be reached. Data is lost on restart, which is the
    correct trade for a degraded mode rather than a hard failure.
    """

    def __init__(self) -> None:
        self.investigations: dict[str, dict] = {}
        self.cases: dict[str, dict] = {}
        self.degraded = True

    async def start(self) -> None:
        """No-op: nothing to initialise."""

    async def close(self) -> None:
        """No-op: nothing to release."""

    async def save_investigation(self, rec: dict) -> None:
        self.investigations[rec["inv_id"]] = dict(rec)

    async def update_investigation(self, inv_id: str, patch: dict) -> None:
        rec = self.investigations.get(inv_id)
        if rec is not None:
            rec.update(patch)

    async def get_investigation(self, inv_id: str) -> dict | None:
        return self.investigations.get(inv_id)

    async def list_investigations(
        self, *, case_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        rows = list(self.investigations.values())
        if case_id:
            rows = [r for r in rows if r.get("case_id") == case_id]
        return rows[-limit:][::-1]

    async def append_step(self, inv_id: str, step: str) -> None:
        rec = self.investigations.get(inv_id)
        if rec is not None:
            rec.setdefault("steps", []).append(step)


# ── Pipeline driver ───────────────────────────────────────────────────────────

def new_id() -> str:
    """Return a short, URL-safe, collision-resistant identifier.

    Returns:
        An 8-character hex identifier.
    """
    return uuid.uuid4().hex[:8]


def shape_report(result: Any, agent_results: list[dict]) -> dict:
    """Shape an APEX AgentResult into the report dict the frontend expects.

    Args:
        result: The APEX ``AgentResult``.
        agent_results: The per-agent serialized payloads.

    Returns:
        The report dictionary consumed by the dashboard.
    """
    out = result.output or {}
    return {
        "summary": (
            f"Investigation complete — {out.get('input_type', 'unknown')}; "
            f"{len(agent_results)} agents, confidence {result.confidence:.0%}"
        ),
        "input_type": out.get("input_type"),
        "agents_activated": out.get("agents_activated", []),
        "entities": out.get("entities", []),
        "signals": out.get("signals", []),
        "graph": out.get("graph", {"nodes": [], "edges": []}),
        "timeline": out.get("timeline", []),
        "markdown": out.get("report_markdown"),
        "confidence": result.confidence,
        "latency_s": result.latency_s,
    }


async def run_pipeline(
    inv_id: str,
    input_value: str,
    *,
    input_type: str | None = None,
    case_id: str | None = None,
    deep: bool = False,
    lang: str | None = None,
) -> dict:
    """Execute one investigation end to end, streaming progress as it goes.

    The store is updated and every agent log line is broadcast to WebSocket
    subscribers as it happens, so the dashboard can render a live trace. A
    failure inside APEX is recorded on the investigation rather than raised,
    so a caller polling ``GET /api/investigate/{id}`` always sees a terminal
    status instead of a task that silently disappeared.

    Args:
        inv_id: Identifier for this run.
        input_value: The raw investigation input.
        input_type: Optional forced input type; auto-detected when omitted.
        case_id: Identifier of the owning case.
        deep: Request the slower, more thorough agent pass.
        lang: Optional language hint for language-aware agents.

    Returns:
        The completed investigation record.
    """
    store = await get_store_handle()
    t0 = time.time()

    await store.update_investigation(
        inv_id, {"status": "running", "started_at": _iso(t0)}
    )

    context: dict[str, Any] = {"case_id": case_id, "deep": deep}
    if input_type:
        context["input_type"] = input_type
    if lang:
        context["lang"] = lang

    steps: list[str] = []

    async def emit(message: str) -> None:
        """Record a step and push it to live subscribers.

        Args:
            message: The step text, already prefixed with the agent name.
        """
        steps.append(message)
        try:
            await store.append_step(inv_id, message)
        except Exception:  # noqa: BLE001 — telemetry must not fail the run
            pass
        await broadcast(inv_id, {"type": "step", "inv_id": inv_id, "message": message})

    context["emit"] = emit

    try:
        from agents.apex import ApexAgent  # lazy: heavy import

        result = await ApexAgent().run(input_value, context=context)
    except Exception as exc:  # noqa: BLE001 — surfaced to the client
        log.exception("pipeline failed for %s", inv_id)
        await store.update_investigation(
            inv_id, {"status": "error", "error": str(exc), "ended_at": _iso()}
        )
        await broadcast(
            inv_id, {"type": "error", "inv_id": inv_id, "error": str(exc)}
        )
        rec = await store.get_investigation(inv_id) or {"inv_id": inv_id}
        return rec

    out = result.output or {}
    agent_results = out.get("agent_results", [])
    report = shape_report(result, agent_results)

    patch = {
        "status": "done",
        "input_type": out.get("input_type"),
        "agents": agent_results,
        "confidence": result.confidence,
        "latency_s": result.latency_s,
        "report": report,
        "routing_confidence": out.get("routing_confidence"),
        "routing_reasoning": out.get("routing_reasoning"),
        "entities": out.get("entities", []),
        "signals": out.get("signals", []),
        "ended_at": _iso(),
    }
    await store.update_investigation(inv_id, patch)

    await broadcast(
        inv_id,
        {"type": "done", "inv_id": inv_id, "report": report, "agents": agent_results},
    )
    return await store.get_investigation(inv_id) or {"inv_id": inv_id, **patch}


async def run_pipeline_background(
    inv_id: str, input_value: str, **kwargs: Any
) -> None:
    """Run a pipeline as a fire-and-forget task, swallowing nothing silently.

    Args:
        inv_id: Identifier for this run.
        input_value: The raw investigation input.
        **kwargs: Forwarded to :func:`run_pipeline` (``input_type``,
            ``case_id``, ``deep``, ``lang``).
    """
    try:
        await run_pipeline(inv_id, input_value, **kwargs)
    except Exception:  # noqa: BLE001 — task boundary
        log.exception("background pipeline crashed for %s", inv_id)


def _iso(ts: float | None = None) -> str:
    """Return an ISO 8601 UTC timestamp.

    Args:
        ts: Epoch seconds; defaults to now.

    Returns:
        An ISO 8601 timestamp with a trailing ``Z``.
    """
    from datetime import datetime, timezone

    moment = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(
        tz=timezone.utc
    )
    return moment.isoformat().replace("+00:00", "Z")


__all__ = [
    "broadcast",
    "get_store_handle",
    "new_id",
    "register_ws",
    "run_pipeline",
    "run_pipeline_background",
    "shape_report",
    "unregister_ws",
    "ws_client_count",
]
