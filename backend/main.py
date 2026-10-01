#!/usr/bin/env python3
"""
main.py — Signal-OS Application Entry Point
===========================================
Wires the platform together: CORS, lifespan, service probes, router mounting,
and the backwards-compatible endpoints the dashboard depends on.

Architecture:
  INPUT ANYTHING → AI ROUTER → MULTI-AGENT ORCHESTRATION
  → TOOL CONNECTORS → SIGNAL EXTRACTION → CORRELATION ENGINE
  → KNOWLEDGE GRAPH → TIMELINE ENGINE → REPORT GENERATION
  → REAL-TIME MONITORING

Route modules under ``backend/api/`` are mounted when present. Each is imported
defensively: a router that is absent (or mid-refactor) degrades to a logged
warning instead of preventing the app from booting, because a platform that
will not start is worse than one missing a convenience endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env before anything that reads configuration.
_env = Path(__file__).parent.parent / ".env"
load_dotenv(_env if _env.exists() else ".env")

from fastapi import FastAPI, WebSocket  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402

import config  # noqa: E402

log = logging.getLogger("signal-os")
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, "INFO"),
    format=getattr(
        config,
        "LOG_FORMAT",
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    ),
)

app = FastAPI(
    title="Signal-OS",
    version=getattr(config, "VERSION", "0.2.0"),
    description="AI-Native Multimodal Intelligence Fusion Platform",
    docs_url="/api/docs" if config.DEBUG else None,
    redoc_url=None,
)

# ── Middleware ────────────────────────────────────────────────────────────────

app.add_middleware(GZipMiddleware, minimum_size=1024)


def _cors_origins() -> list[str]:
    """Resolve the CORS allowlist from configuration.

    Returns:
        The permitted origins. A wildcard is only honoured in development,
        and never together with credentials — the previous configuration
        shipped ``allow_origins=["*"]`` with ``allow_credentials=True``, which
        is both a wildcard-exposure hole and an invalid combination.
    """
    raw = getattr(config, "CORS_ORIGINS", "") or ""
    origins = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    if origins:
        return origins
    if config.DEBUG:
        # Local development convenience only.
        return [
            "http://localhost:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:3001",
        ]
    log.warning("CORS_ORIGINS unset outside debug — browser clients will be blocked")
    return []


_origins = _cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    # Credentials only make sense against a concrete allowlist.
    allow_credentials=bool(_origins) and "*" not in _origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=[
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
    ],
    max_age=600,
)


# ── Service probes ────────────────────────────────────────────────────────────


async def _probe_services() -> dict[str, bool]:
    """Probe every configured backing service concurrently.

    Returns:
        A mapping of service name to reachability. Empty on a serverless
        runtime, where no persistent infrastructure is reachable.
    """
    if os.getenv("VERCEL"):
        # Each cold start would otherwise burn seconds per probe against
        # services that will never answer; every agent degrades gracefully
        # without them.
        log.info("Signal-OS starting on a serverless runtime — skipping infra probes.")
        return {}

    log.info("Probing backing services…")

    async def _pg() -> None:
        import psycopg2

        conn = psycopg2.connect(config.DATABASE_URL, connect_timeout=3)
        conn.close()

    async def _redis() -> None:
        import redis.asyncio as aioredis

        client = aioredis.from_url(config.REDIS_URL, socket_connect_timeout=3)
        try:
            await client.ping()
        finally:
            await client.aclose()

    async def _qdrant() -> None:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{config.QDRANT_URL}/readyz", timeout=3)
            resp.raise_for_status()

    async def _minio() -> None:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://{config.MINIO_ENDPOINT}/minio/health/ready", timeout=3
            )
            resp.raise_for_status()

    async def _neo4j() -> None:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD)
            if config.NEO4J_PASSWORD
            else None,
        )
        try:
            driver.verify_connectivity()
        finally:
            driver.close()

    checks: dict[str, Any] = {
        "PostgreSQL": _pg,
        "Redis": _redis,
        "Qdrant": _qdrant,
        "MinIO": _minio,
        "Neo4j": _neo4j,
    }
    names = list(checks)
    results = await asyncio.gather(
        *(checks[name]() for name in names), return_exceptions=True
    )
    health: dict[str, bool] = {}
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            log.warning("  ✗ %s  %s", name, result)
            health[name] = False
        else:
            log.info("  ✓ %s", name)
            health[name] = True
    return health


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Manage application startup and shutdown.

    Args:
        _: The FastAPI application (unused).

    Yields:
        ``None`` once startup has completed.
    """
    services = await _probe_services()

    try:
        from runtime import get_store_handle

        store = await get_store_handle()
        if getattr(store, "degraded", False):
            log.warning("store running degraded — investigations are not persisted")
        else:
            log.info("store ready")
    except Exception as exc:  # noqa: BLE001 — never block startup on storage
        log.warning("store unavailable: %s", exc)

    if services:
        up = sum(1 for v in services.values() if v)
        log.info("Startup complete — %d/%d services reachable.", up, len(services))

    yield

    try:
        from llm import close_llm

        await close_llm()
    except Exception:  # noqa: BLE001
        pass
    try:
        from runtime import get_store_handle

        await (await get_store_handle()).close()
    except Exception:  # noqa: BLE001
        pass
    log.info("Signal-OS shut down cleanly.")


app.router.lifespan_context = lifespan


# ── Router mounting (defensive) ───────────────────────────────────────────────

_MOUNTED: list[str] = []
_MOUNT_ERRORS: dict[str, str] = {}


def _mount(module_name: str, router_attr: str = "router") -> None:
    """Mount a route module when it is importable.

    Args:
        module_name: Dotted module path relative to ``backend``.
        router_attr: Attribute on the module holding the APIRouter.
    """
    import importlib

    try:
        module = importlib.import_module(module_name)
        app.include_router(getattr(module, router_attr))
        _MOUNTED.append(module_name)
    except Exception as exc:  # noqa: BLE001 — a missing router is not fatal
        _MOUNT_ERRORS[module_name] = str(exc)
        log.warning("router %s unavailable: %s", module_name, exc)


for _module in (
    "api.ops",
    "api.investigate",
    "api.cases",
    "api.agents",
    "api.reports",
):
    _mount(_module)


# ── Shared runtime ────────────────────────────────────────────────────────────

from runtime import (  # noqa: E402
    get_store_handle,
    new_id,
    register_ws,
    run_pipeline,
    run_pipeline_background,
    unregister_ws,
)


def _investigate_model() -> Any:
    """Return the canonical request model, falling back to a local shim.

    Returns:
        A pydantic model class for the investigate endpoint.
    """
    try:
        from schemas import InvestigateRequest

        return InvestigateRequest
    except Exception:  # noqa: BLE001 — schemas.py may not exist yet
        from pydantic import BaseModel, Field

        class InvestigateRequest(BaseModel):  # type: ignore[no-redef]
            """Investigation submission payload."""

            input: str = Field(..., max_length=8192)
            input_type: str | None = Field(default=None, max_length=64)
            case_id: str | None = Field(default=None, max_length=64)

        return InvestigateRequest


_InvestigateRequest = _investigate_model()


# ── Backwards-compatible endpoints ────────────────────────────────────────────
# These paths predate the router split and are relied on by the dashboard and
# documented in the README. They delegate to the same runtime helpers the
# routers use, and are registered after the routers so an included router wins
# any genuine path collision.

@app.get("/api/health")
async def health() -> dict:
    """Liveness probe.

    Returns:
        A status document, including which routers mounted.
    """
    return {
        "status": "ok",
        "platform": "signal-os",
        "version": app.version,
        "routers": _MOUNTED,
        "routers_unavailable": _MOUNT_ERRORS,
    }


@app.post("/api/investigate")
async def submit_investigation(req: _InvestigateRequest) -> dict:
    """Queue an investigation and return immediately.

    Args:
        req: The investigation submission.

    Returns:
        The assigned investigation and case identifiers.
    """
    store = await get_store_handle()
    inv_id = new_id()
    case_id = req.case_id or new_id()
    await store.save_investigation(
        {
            "inv_id": inv_id,
            "case_id": case_id,
            "input": req.input,
            "input_type": req.input_type,
            "status": "queued",
            "steps": [],
            "report": None,
        }
    )
    asyncio.create_task(
        run_pipeline_background(
            inv_id, req.input, input_type=req.input_type, case_id=case_id
        )
    )
    return {"inv_id": inv_id, "case_id": case_id, "status": "queued"}


@app.get("/api/investigate/{inv_id}")
async def get_investigation(inv_id: str) -> dict:
    """Return one investigation's current state.

    Args:
        inv_id: Investigation identifier.

    Returns:
        The investigation record, or an error document when unknown.
    """
    store = await get_store_handle()
    rec = await store.get_investigation(inv_id)
    if rec is None:
        return {"error": "not found", "inv_id": inv_id}
    return rec


@app.post("/api/investigate-sync")
async def submit_investigation_sync(req: _InvestigateRequest) -> dict:
    """Run an investigation to completion within a single request.

    Serverless platforms isolate each invocation, so a background task cannot
    be relied on to outlive the response and a later GET may land on a
    different instance. This path blocks until APEX finishes and returns the
    whole report in one round trip; WebSocket streaming is unavailable there.

    Args:
        req: The investigation submission.

    Returns:
        The completed report.
    """
    store = await get_store_handle()
    inv_id = new_id()
    case_id = req.case_id or new_id()
    await store.save_investigation(
        {
            "inv_id": inv_id,
            "case_id": case_id,
            "input": req.input,
            "input_type": req.input_type,
            "status": "queued",
            "steps": [],
            "report": None,
        }
    )
    try:
        rec = await run_pipeline(
            inv_id, req.input, input_type=req.input_type, case_id=case_id
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller
        log.exception("sync pipeline failed for %s", inv_id)
        return {
            "inv_id": inv_id,
            "case_id": case_id,
            "status": "error",
            "error": str(exc),
        }
    return {
        "inv_id": inv_id,
        "case_id": case_id,
        "status": rec.get("status", "done"),
        "input": req.input,
        "input_type": rec.get("input_type"),
        "agents": rec.get("agents", []),
        "report": rec.get("report"),
    }


@app.get("/api/investigations")
async def list_investigations(limit: int = 50, offset: int = 0) -> list[dict]:
    """List recent investigations, newest first.

    Args:
        limit: Maximum rows to return.
        offset: Rows to skip.

    Returns:
        A summary list of investigations.
    """
    store = await get_store_handle()
    rows = await store.list_investigations(limit=limit + offset)
    window = rows[offset : offset + max(1, min(limit, 200))]
    return [
        {
            "inv_id": r.get("inv_id"),
            "case_id": r.get("case_id"),
            "status": r.get("status"),
            "input": str(r.get("input", ""))[:80],
            "input_type": r.get("input_type"),
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"),
        }
        for r in window
    ]


@app.post("/api/search")
async def run_search(req: dict) -> dict:
    """Run SCOUT directly against a query.

    Args:
        req: A mapping with ``query`` and optional ``engines`` / ``use_dorks``.

    Returns:
        The search agent's result.
    """
    from agents.scout import ScoutAgent

    query = str(req.get("query", "")).strip()
    if not query:
        return {"error": "query is required", "agent": "SCOUT", "status": "error"}
    scout = ScoutAgent()
    result = await scout.run(
        query,
        context={
            "engines": req.get("engines") or ["google", "bing", "yandex"],
            "use_dorks": bool(req.get("use_dorks", True)),
        },
    )
    return {
        "query": query,
        "agent": result.agent,
        "status": result.status,
        "output": result.output,
        "confidence": result.confidence,
        "reasoning": result.reasoning,
        "latency_s": result.latency_s,
        "steps": scout._steps,
        "error": result.error,
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────


@app.websocket("/ws/pipeline/{inv_id}")
async def ws_pipeline(websocket: WebSocket, inv_id: str) -> None:
    """Stream pipeline progress for one investigation.

    Args:
        websocket: The accepted connection. The annotation must be
            ``WebSocket`` — FastAPI decides whether a parameter belongs to the
            handshake from its type, so a looser annotation turns this into a
            required query parameter and every connection is rejected.
        inv_id: Investigation identifier.
    """
    from fastapi import WebSocketDisconnect

    allowed = getattr(config, "WS_ALLOWED_ORIGINS", "") or ""
    if allowed:
        permitted = {o.strip().rstrip("/") for o in allowed.split(",") if o.strip()}
        origin = (websocket.headers.get("origin") or "").rstrip("/")
        if origin and origin not in permitted:
            log.warning("websocket origin rejected: %s", origin)
            await websocket.close(code=1008)
            return

    await websocket.accept()
    await register_ws(inv_id, websocket)

    store = await get_store_handle()
    rec = await store.get_investigation(inv_id)
    if rec and rec.get("status") == "done":
        await websocket.send_text(
            json.dumps(
                {"type": "done", "inv_id": inv_id, "report": rec.get("report")}
            )
        )
        await unregister_ws(inv_id, websocket)
        await websocket.close()
        return

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — client vanished
        pass
    finally:
        await unregister_ws(inv_id, websocket)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=getattr(config, "HOST", "0.0.0.0"),
        port=getattr(config, "PORT", 8766),
        reload=config.DEBUG,
        log_level=str(getattr(config, "LOG_LEVEL", "INFO")).lower(),
        workers=1 if config.DEBUG else getattr(config, "WORKERS", 4),
    )
