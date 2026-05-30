#!/usr/bin/env python3
"""
Signal-OS — FastAPI Backend
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
import uuid
from typing import Optional, Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agents.apex import ApexAgent
from agents.scout import ScoutAgent

# Load .env before anything reads os.getenv()
load_dotenv()

# ── Config (all from environment) ─────────────────────────────────────────────

DATABASE_URL  = os.getenv("DATABASE_URL",  "postgresql://signal:signal@localhost:5432/signal_os")
REDIS_URL     = os.getenv("REDIS_URL",     "redis://localhost:6379")
NEO4J_URI     = os.getenv("NEO4J_URI",     "bolt://localhost:7687")
NEO4J_USER    = os.getenv("NEO4J_USER",    "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
QDRANT_URL    = os.getenv("QDRANT_URL",    "http://localhost:6333")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
SECRET_KEY    = os.getenv("SECRET_KEY",    "change-me-in-production")
DEBUG         = os.getenv("DEBUG", "true").lower() == "true"

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("signal-os")

app = FastAPI(title="Signal-OS", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Startup: probe connected services ────────────────────────────────────────

@app.on_event("startup")
async def _startup_checks():
    log.info("Signal-OS starting up — probing services…")

    # PostgreSQL
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
        conn.close()
        log.info("  ✓ PostgreSQL  %s", DATABASE_URL.split("@")[-1])
    except Exception as e:
        log.warning("  ✗ PostgreSQL  %s", e)

    # Redis
    try:
        import redis as _redis
        r = _redis.from_url(REDIS_URL, socket_connect_timeout=3)
        r.ping()
        log.info("  ✓ Redis       %s", REDIS_URL)
    except Exception as e:
        log.warning("  ✗ Redis       %s", e)

    # Neo4j
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD) if NEO4J_PASSWORD else None)
        driver.verify_connectivity()
        driver.close()
        log.info("  ✓ Neo4j       %s", NEO4J_URI)
    except Exception as e:
        log.warning("  ✗ Neo4j       %s", e)

    # Qdrant
    try:
        import httpx
        r = httpx.get(f"{QDRANT_URL}/readyz", timeout=3)
        r.raise_for_status()
        log.info("  ✓ Qdrant      %s", QDRANT_URL)
    except Exception as e:
        log.warning("  ✗ Qdrant      %s", e)

    # MinIO
    try:
        import httpx
        r = httpx.get(f"http://{MINIO_ENDPOINT}/minio/health/ready", timeout=3)
        r.raise_for_status()
        log.info("  ✓ MinIO       %s", MINIO_ENDPOINT)
    except Exception as e:
        log.warning("  ✗ MinIO       %s", e)

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

async def _run_pipeline(inv_id: str, request: InvestigateRequest):
    """Universal intelligence pipeline driven by APEX.

    APEX routes the input, activates the matching agent swarm concurrently, and
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
    INVESTIGATIONS[inv_id]["report"] = {
        "summary": (
            f"Investigation complete — {out.get('input_type', 'unknown')}; "
            f"{len(agent_results)} agents, confidence {result.confidence:.0%}"
        ),
        "input_type": out.get("input_type"),
        "agents_activated": out.get("agents_activated", []),
        "entities": out.get("entities", []),
        "signals": out.get("signals", []),
        "confidence": result.confidence,
        "latency_s": result.latency_s,
    }
    await _broadcast(inv_id, {
        "type": "done", "inv_id": inv_id,
        "report": INVESTIGATIONS[inv_id]["report"],
        "agents": agent_results,
    })

# ── HTTP endpoints ────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "platform": "signal-os", "version": "0.1.0"}

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run("main:app", host=args.host, port=args.port, reload=True, log_level="info")
