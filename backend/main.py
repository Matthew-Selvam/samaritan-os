#!/usr/bin/env python3
"""
Samaritan OS — FastAPI Backend
============================
AI-Native Multimodal Intelligence Fusion Platform.

Architecture:
  INPUT ANYTHING → AI ROUTER → MULTI-AGENT ORCHESTRATION
  → TOOL CONNECTORS → SIGNAL EXTRACTION → CORRELATION ENGINE
  → KNOWLEDGE GRAPH → TIMELINE ENGINE → REPORT GENERATION
  → REAL-TIME MONITORING

Endpoints:
  POST /api/investigate    — submit entity/file/text for full pipeline
  POST /api/search         — run search agent (dorks, federation)
  POST /api/compare        — model arena side-by-side comparison
  GET  /api/cases          — list investigation cases
  GET  /api/entities       — entity graph nodes
  GET  /api/health         — liveness check
  WS   /ws/pipeline/{id}   — real-time pipeline trace
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from typing import Optional, Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pathlib import Path

# Load .env first, then import config
_env = Path(__file__).parent.parent / ".env"
load_dotenv(_env if _env.exists() else ".env")

import config
from agents.apex import ApexAgent
from agents.scout import ScoutAgent

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format=config.LOG_FORMAT,
)
log = logging.getLogger("samaritan-os")

app = FastAPI(
    title="Samaritan OS",
    version="0.1.0",
    description="AI-Native Multimodal Intelligence Fusion Platform",
    docs_url="/api/docs" if config.DEBUG else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Startup: probe connected services ────────────────────────────────────────

async def _probe_service(name: str, test_fn) -> bool:
    """Generic service probe."""
    try:
        await test_fn() if asyncio.iscoroutinefunction(test_fn) else test_fn()
        log.info("  ✓ %s", name)
        return True
    except Exception as e:
        log.warning("  ✗ %s  %s", name, e)
        return False


@app.on_event("startup")
async def _startup_checks():
    if os.getenv("VERCEL"):
        # Serverless: no persistent infra is reachable here, and every cold
        # start would otherwise burn ~3s per probe against services that will
        # never answer. Every agent already degrades gracefully without them.
        log.info("Samaritan OS starting up on Vercel — skipping infra probes.")
        return

    log.info("Samaritan OS starting up — probing services…")

    # PostgreSQL
    async def _pg():
        import psycopg2
        conn = psycopg2.connect(config.DATABASE_URL, connect_timeout=3)
        conn.close()
    await _probe_service("PostgreSQL", _pg)

    # Redis
    async def _redis():
        import redis as _redis
        r = _redis.from_url(config.REDIS_URL, socket_connect_timeout=3)
        r.ping()
    await _probe_service("Redis", _redis)

    # Neo4j
    async def _neo4j():
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD) if config.NEO4J_PASSWORD else None
        )
        driver.verify_connectivity()
        driver.close()
    await _probe_service("Neo4j", _neo4j)

    # Qdrant
    async def _qdrant():
        import httpx
        r = await httpx.AsyncClient().get(f"{config.QDRANT_URL}/readyz", timeout=3)
        r.raise_for_status()
    await _probe_service("Qdrant", _qdrant)

    # MinIO
    async def _minio():
        import httpx
        r = await httpx.AsyncClient().get(
            f"http://{config.MINIO_ENDPOINT}/minio/health/ready", timeout=3
        )
        r.raise_for_status()
    await _probe_service("MinIO", _minio)

    log.info("Startup checks complete.")


# ── In-memory stores (replace with Postgres + Neo4j in production) ────────

INVESTIGATIONS: dict[str, dict] = {}
WS_CLIENTS: dict[str, list[WebSocket]] = {}

# ── Models ────────────────────────────────────────────────────────────────

class InvestigateRequest(BaseModel):
    """Universal input — can be text, username, email, domain, or file path."""
    input: str
    input_type: Optional[str] = None   # auto-detected if None
    case_id: Optional[str] = None      # add to existing case, or create new

class SearchRequest(BaseModel):
    query: str
    engines: list[str] = ["google", "bing", "yandex"]
    use_dorks: bool = True

class NameSearchRequest(BaseModel):
    name: str
    location: Optional[str] = None
    case_id: Optional[str] = None

# ── Broadcast helper ──────────────────────────────────────────────────────

async def _broadcast(channel: str, payload: dict):
    dead = []
    for ws in WS_CLIENTS.get(channel, []):
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)
    for ws in dead:
        WS_CLIENTS[channel].remove(ws)

# ── Pipeline ──────────────────────────────────────────────────────────────

def _build_report(result, agent_results: list[dict]) -> dict:
    """Shape an ApexAgent AgentResult into the report dict the frontend expects."""
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


async def _run_pipeline(inv_id: str, request: InvestigateRequest):
    """Universal intelligence pipeline driven by APEX (background-task variant).

    Used by the async submit/poll/WebSocket flow, which requires a long-lived
    process (local dev, Docker, Railway/Render/a VPS) so the in-memory
    INVESTIGATIONS dict and the background task survive between requests. APEX
    routes the input, activates the matching agent swarm concurrently, and
    synthesizes their outputs. Every agent log line is streamed to WS clients as
    a {"type": "step", "message": ...} event; a {"type": "done"} event with the
    final report closes the run.
    """
    INVESTIGATIONS[inv_id]["status"] = "running"
    steps: list[str] = []

    async def emit(msg: str):
        steps.append(msg)
        INVESTIGATIONS[inv_id]["steps"] = steps
        await _broadcast(inv_id, {"type": "step", "inv_id": inv_id, "message": msg})

    try:
        apex = ApexAgent()
        result = await apex.run(
            request.input,
            context={"emit": emit, "input_type": request.input_type, "case_id": request.case_id},
        )
    except Exception as e:  # noqa: BLE001 — surface orchestration failure to the client
        log.exception("pipeline failed for %s", inv_id)
        INVESTIGATIONS[inv_id]["status"] = "error"
        INVESTIGATIONS[inv_id]["error"] = str(e)
        await _broadcast(inv_id, {"type": "error", "inv_id": inv_id, "error": str(e)})
        return

    out = result.output or {}
    agent_results = out.get("agent_results", [])

    # ── Persist every agent's output / confidence / latency ───────────────────
    INVESTIGATIONS[inv_id]["status"] = "done"
    INVESTIGATIONS[inv_id]["input_type"] = out.get("input_type")
    INVESTIGATIONS[inv_id]["agents"] = agent_results
    INVESTIGATIONS[inv_id]["confidence"] = result.confidence
    INVESTIGATIONS[inv_id]["latency_s"] = result.latency_s
    INVESTIGATIONS[inv_id]["report"] = _build_report(result, agent_results)
    await _broadcast(inv_id, {
        "type": "done", "inv_id": inv_id,
        "report": INVESTIGATIONS[inv_id]["report"],
        "agents": agent_results,
    })

# ── HTTP endpoints ────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "platform": "samaritan-os", "version": "0.1.0"}

@app.post("/api/investigate")
async def submit_investigation(req: InvestigateRequest):
    inv_id = str(uuid.uuid4())[:8]
    case_id = req.case_id or str(uuid.uuid4())[:8]
    INVESTIGATIONS[inv_id] = {
        "inv_id": inv_id, "case_id": case_id,
        "input": req.input, "input_type": req.input_type,
        "status": "queued", "steps": [], "report": None,
    }
    asyncio.ensure_future(_run_pipeline(inv_id, req))
    return {"inv_id": inv_id, "case_id": case_id, "status": "queued"}

@app.get("/api/investigate/{inv_id}")
async def get_investigation(inv_id: str):
    if inv_id not in INVESTIGATIONS:
        return {"error": "not found"}
    return INVESTIGATIONS[inv_id]

@app.post("/api/investigate-sync")
async def submit_investigation_sync(req: InvestigateRequest):
    """Run a full investigation to completion within a single request/response.

    Serverless platforms (Vercel Functions) isolate each invocation — there is no
    guarantee a background task survives past the response, and no guarantee a
    later GET lands on the same instance as the in-memory INVESTIGATIONS dict
    that a POST wrote to. This endpoint sidesteps both problems by never
    returning until APEX has finished, so the full report comes back in one
    round trip. No WebSocket step-streaming (not applicable outside a
    persistent process) — the frontend just awaits the response.
    """
    inv_id = str(uuid.uuid4())[:8]
    case_id = req.case_id or str(uuid.uuid4())[:8]
    try:
        apex = ApexAgent()
        result = await apex.run(
            req.input,
            context={"input_type": req.input_type, "case_id": case_id},
        )
    except Exception as e:  # noqa: BLE001 — surface orchestration failure to the client
        log.exception("sync pipeline failed for %s", inv_id)
        return {"inv_id": inv_id, "case_id": case_id, "status": "error", "error": str(e)}

    agent_results = (result.output or {}).get("agent_results", [])
    return {
        "inv_id": inv_id, "case_id": case_id, "status": "done",
        "input": req.input, "input_type": (result.output or {}).get("input_type"),
        "agents": agent_results,
        "report": _build_report(result, agent_results),
    }

@app.get("/api/investigations")
async def list_investigations():
    return [
        {"inv_id": k, "status": v["status"], "input": v.get("input", "")[:80]}
        for k, v in INVESTIGATIONS.items()
    ]

@app.post("/api/search")
async def run_search(req: SearchRequest):
    """Run SCOUT directly — dork generation + (future) search federation."""
    scout = ScoutAgent()
    result = await scout.run(req.query, context={"engines": req.engines, "use_dorks": req.use_dorks})
    return {
        "query": req.query,
        "agent": result.agent,
        "status": result.status,
        "output": result.output,
        "confidence": result.confidence,
        "reasoning": result.reasoning,
        "latency_s": result.latency_s,
        "steps": scout._steps,
        "error": result.error,
    }

# ── Photo search endpoint ─────────────────────────────────────────────────

@app.post("/api/photo-search")
async def photo_search(file: UploadFile = File(...)):
    """Upload a photo → run IRIS deanonymization pipeline."""
    # Save upload to temp file
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename or ".jpg")[1])
    try:
        shutil.copyfileobj(file.file, tmp)
        tmp.close()

        inv_id = str(uuid.uuid4())[:8]
        case_id = str(uuid.uuid4())[:8]
        INVESTIGATIONS[inv_id] = {
            "inv_id": inv_id, "case_id": case_id,
            "input": f"photo:{file.filename}", "input_type": "photo",
            "status": "queued", "steps": [], "report": None,
        }

        req = InvestigateRequest(input=tmp.name, input_type="photo", case_id=case_id)
        asyncio.ensure_future(_run_pipeline(inv_id, req))
        return {"inv_id": inv_id, "case_id": case_id, "status": "queued", "filename": file.filename}
    except Exception as e:
        return {"error": str(e)}

# ── Name search endpoint ──────────────────────────────────────────────────

@app.post("/api/name-search")
async def name_search(req: NameSearchRequest):
    """Search by person name → PRISM + SCOUT + INK pipeline."""
    inv_id = str(uuid.uuid4())[:8]
    case_id = req.case_id or str(uuid.uuid4())[:8]
    INVESTIGATIONS[inv_id] = {
        "inv_id": inv_id, "case_id": case_id,
        "input": req.name, "input_type": "person_name",
        "status": "queued", "steps": [], "report": None,
    }
    investigate_req = InvestigateRequest(
        input=req.name, input_type="person_name", case_id=case_id,
    )
    asyncio.ensure_future(_run_pipeline(inv_id, investigate_req))
    return {"inv_id": inv_id, "case_id": case_id, "status": "queued", "name": req.name}

# ── OPSEC endpoints ───────────────────────────────────────────────────────

@app.get("/api/opsec/status")
async def opsec_status():
    """OPSEC status — Tor connectivity, current exit IP, request count."""
    try:
        from opsec import get_opsec_client
        client = await get_opsec_client()
        return await client.status()
    except Exception as e:
        return {"opsec_enabled": False, "error": str(e)}

@app.post("/api/opsec/newcircuit")
async def opsec_new_circuit():
    """Request a new Tor circuit (new exit node)."""
    try:
        from opsec import get_opsec_client
        client = await get_opsec_client()
        return await client.new_circuit()
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── WebSocket ─────────────────────────────────────────────────────────────

@app.websocket("/ws/pipeline/{inv_id}")
async def ws_pipeline(websocket: WebSocket, inv_id: str):
    await websocket.accept()
    WS_CLIENTS.setdefault(inv_id, []).append(websocket)
    if inv_id in INVESTIGATIONS and INVESTIGATIONS[inv_id]["status"] == "done":
        await websocket.send_text(json.dumps({
            "type": "done", "inv_id": inv_id,
            "report": INVESTIGATIONS[inv_id]["report"],
        }))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        WS_CLIENTS[inv_id] = [ws for ws in WS_CLIENTS.get(inv_id, []) if ws != websocket]

# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import uvicorn  # only needed for local/self-hosted runs, not on Vercel
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--reload", action="store_true", default=config.DEBUG)
    args = parser.parse_args()
    uvicorn.run("main:app", host=args.host, port=args.port, reload=args.reload,
                log_level=config.LOG_LEVEL.lower(), workers=1 if args.reload else config.WORKERS)
