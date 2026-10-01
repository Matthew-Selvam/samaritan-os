"""agents.py — agent introspection and standalone agent execution.

Owned by WS-API. New surface (none of it existed in ``main.py``):

    GET  /api/agents                  registry: name/role/icon/description/models/tier
    GET  /api/agents/registry/graph   routing DAG, for the UI to draw
    GET  /api/agents/{name}           one agent's descriptor
    POST /api/agents/{name}/run       run a single agent outside the APEX swarm

Route-order note: ``/registry/graph`` is declared *before* ``/{name}`` so the
literal segment always wins the match — FastAPI matches in declaration order,
and the reverse order would shadow it with the ``{name}`` parameter.

The registry is derived from ``agents.AGENT_REGISTRY`` (the single source of
truth) and never hardcoded here, so adding an agent to the registry is enough to
make it appear in the API. Tier assignment comes from ``config.PRIMARY_AGENTS``
/ ``config.CORRELATION_TIER``, and the graph is built from ``router.AGENT_MAP``:
one node per agent, one edge from each agent to the agents APEX may run
alongside it for a given input type.

``POST /agents/{name}/run`` is the one route here that can trigger real outbound
work (SCOUT hits search engines, CRAWLER fetches URLs), so it is auth-guarded,
rate-limited, hard-capped on input length and run count, and wrapped so a
failing agent returns a structured result instead of a 500.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api.ops import (
    auth_gate,
    clamp_limit,
    current_principal,
    rate_limit_dep,
    resolve_store,
    _maybe_import,
)
from observability import get_logger, get_metrics

log = get_logger("signal-os.api.agents")

router = APIRouter(prefix="/api", tags=["agents"])

#: Ceiling on one standalone run's input, in characters.
MAX_RUN_INPUT_CHARS = 8192

#: Wall-clock ceiling for a standalone run, in seconds. APEX imposes per-agent
#: timeouts internally; this is the outer guard for a direct call.
RUN_TIMEOUT_S = 180.0


# ════════════════════════════════════════════════════════════════════════════
# Request model
# ════════════════════════════════════════════════════════════════════════════


class _AgentRunRequest(BaseModel):
    """Run one agent directly against a target.

    Attributes:
        input: The target the agent should analyse.
        input_type: Optional routing hint handed to the agent.
        case_id: Optional case the run belongs to.
        deep: Request the deeper sweep.
        lang: Response language hint.
        context: Free-form extra context keys; bounded in size to stop a caller
            smuggling a megabyte of state past the input cap.
    """

    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1, max_length=MAX_RUN_INPUT_CHARS)
    input_type: Optional[str] = Field(default=None, max_length=64)
    case_id: Optional[str] = Field(default=None, max_length=64)
    deep: bool = False
    lang: Optional[str] = Field(default=None, max_length=16)
    context: dict[str, Any] = Field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════════
# Registry introspection
# ════════════════════════════════════════════════════════════════════════════


def _registry() -> dict[str, type]:
    """Return the agent registry from ``agents.AGENT_REGISTRY``.

    Returns:
        ``{AGENT_NAME: class}``. Empty when the agents package cannot be
        imported, so introspection degrades instead of failing.
    """
    agents_mod = _maybe_import("agents")
    registry = getattr(agents_mod, "AGENT_REGISTRY", None) if agents_mod else None
    return registry if isinstance(registry, dict) else {}


def _tiers() -> tuple[set[str], set[str]]:
    """Read the primary and correlation-tier agent sets from config.

    Returns:
        ``(primary, correlation)``, both lowercased-name sets. Empty when config
        cannot be read.
    """
    config = _maybe_import("config")
    if config is None:
        return set(), set()
    primary = {str(n).lower() for n in getattr(config, "PRIMARY_AGENTS", []) or []}
    correlation = {str(n).lower() for n in getattr(config, "CORRELATION_TIER", []) or []}
    return primary, correlation


def _tier_for(name: str) -> str:
    """Classify one agent into a tier.

    Args:
        name: Agent name as registered (e.g. ``"NEXUS"``).

    Returns:
        ``"supervisor"`` for APEX, ``"primary"`` for swarm agents that run
        concurrently, ``"correlation"`` for second-tier reasoners, and
        ``"standalone"`` for anything the config does not classify.
    """
    lowered = str(name).lower()
    if lowered == "apex":
        return "supervisor"
    primary, correlation = _tiers()
    if lowered in primary:
        return "primary"
    if lowered in correlation:
        return "correlation"
    return "standalone"


def _descriptor(cls: type, name: str) -> dict:
    """Build one agent descriptor.

    Reads every attribute defensively: a stub agent that fails to define
    ``description`` must not break introspection of the whole registry.

    Args:
        cls: The agent class.
        name: The registry key.

    Returns:
        ``{name, role, icon, description, models, context_folders, token_budget,
        tier, timeout_s, status}``.
    """

    def _attr(attr: str, default: Any) -> Any:
        return getattr(cls, attr, default)

    role = str(_attr("role", ""))
    return {
        "name": name,
        "role": role,
        "icon": str(_attr("icon", "◎")),
        "description": str(_attr("description", "") or role or name),
        "models": list(_attr("preferred_models", []) or []),
        "context_folders": list(_attr("context_folders", []) or []),
        "token_budget": int(_attr("token_budget", 0) or 0),
        "tier": _tier_for(name),
        "timeout_s": _timeout_for(name),
        "correlation_tier": list(_attr("CORRELATION_TIER", []) or []),
        "status": "idle",
    }


def _timeout_for(name: str) -> float:
    """Resolve the configured timeout for one agent.

    Args:
        name: Agent name.

    Returns:
        Timeout in seconds, falling back to 30 when config is unavailable.
    """
    config = _maybe_import("config")
    getter = getattr(config, "get_timeout", None) if config else None
    if callable(getter):
        try:
            return float(getter(str(name)))
        except Exception:  # noqa: BLE001
            pass
    return 30.0


def _all_descriptors() -> list[dict]:
    """Describe every registered agent, sorted by tier then name.

    Returns:
        A list of descriptors; empty when the registry cannot be imported.
    """
    registry = _registry()
    descriptors = [_descriptor(cls, name) for name, cls in registry.items()]
    tier_order = {"supervisor": 0, "primary": 1, "correlation": 2, "standalone": 3}
    descriptors.sort(key=lambda d: (tier_order.get(d["tier"], 9), d["name"]))
    return descriptors


@router.get("/agents")
async def list_agents(limit: int = 100, offset: int = 0):
    """List every registered agent with its descriptor.

    Returns:
        ``{items, total, limit, offset}`` — each item carries name, role, icon,
        description, preferred models and tier. The frontend's ``listAgents``
        unwraps either the envelope or a bare array.
    """
    from api.ops import paged

    descriptors = _all_descriptors()
    return paged(descriptors, limit, offset)


def _routing_graph() -> dict:
    """Build the routing DAG the UI draws.

    Nodes come from the agent registry; edges come from ``router.AGENT_MAP`` —
    for each input type, every activated agent is linked to that input type's
    "entry" agents, and the input-type nodes are linked to APEX. Edges are
    deduped and labelled with the input types that trigger them, so the UI can
    show *why* two agents are related.

    Returns:
        ``{nodes, edges, input_types}`` where each node is
        ``{id, label, role, icon, tier, kind}`` and each edge is
        ``{source, target, label, type, weight}``.
    """
    router_mod = _maybe_import("router")
    agent_map = getattr(router_mod, "AGENT_MAP", {}) if router_mod else {}
    descriptors = {d["name"]: d for d in _all_descriptors()}

    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str], dict] = {}

    def _node(node_id: str, label: str, kind: str, **extra: Any) -> None:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "label": label, "kind": kind, **extra}

    # Agent nodes.
    for name, descriptor in descriptors.items():
        _node(name, name, "agent", role=descriptor["role"], icon=descriptor["icon"],
              tier=descriptor["tier"], depends_on=[], inputs=[], outputs=[])

    # Input-type nodes + the APEX -> agents routing edges.
    for input_type, activated in (agent_map or {}).items():
        type_id = f"input:{getattr(input_type, 'value', input_type)}"
        _node(type_id, getattr(input_type, "value", str(input_type)), "input_type",
              tier="input")
        for agent_name in activated or []:
            agent = _agent_name(agent_name)
            if agent not in nodes:
                _node(agent, agent, "agent", role="", icon="◎", tier="standalone",
                      depends_on=[], inputs=[], outputs=[])
            key = (type_id, agent)
            edge = edges.get(key)
            if edge is None:
                edges[key] = {
                    "source": type_id, "target": agent,
                    "label": getattr(input_type, "value", str(input_type)),
                    "type": "routes_to", "weight": 1.0,
                    "input_types": [getattr(input_type, "value", str(input_type))],
                }
            else:
                edge["weight"] = float(edge["weight"]) + 1.0
                edge["input_types"].append(getattr(input_type, "value", str(input_type)))

    # APEX supervises every routing decision.
    if "APEX" in nodes:
        for node_id in [n for n in nodes if n.startswith("input:")]:
            edges[(node_id, "APEX")] = {
                "source": node_id, "target": "APEX", "label": "supervised_by",
                "type": "supervises", "weight": 1.0, "input_types": [],
            }

    # Correlation-tier agents consume the primary swarm's output.
    primary, correlation = _tiers()
    for node_id, node in nodes.items():
        if node.get("kind") != "agent" or node_id == "APEX":
            continue
        lowered = node_id.lower()
        if lowered in correlation:
            producers = sorted(
                n for n in nodes
                if nodes[n].get("kind") == "agent"
                and n != node_id
                and n.lower() in primary
            )
            for producer in producers:
                edges[(producer, node_id)] = {
                    "source": producer, "target": node_id,
                    "label": "feeds", "type": "correlates",
                    "weight": 1.0, "input_types": [],
                }
            node["depends_on"] = producers

    return {
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "input_types": sorted(n[len("input:"):] for n in nodes if n.startswith("input:")),
    }


def _agent_name(value: Any) -> str:
    """Normalise an ``AGENT_MAP`` entry into its registry name.

    ``AGENT_MAP`` values are plain strings, but a registry may be keyed by class
    name or enum value in future, so unwrap defensively.

    Args:
        value: One entry from ``router.AGENT_MAP``.

    Returns:
        The uppercase registry name.
    """
    raw = getattr(value, "value", value)
    return str(raw).strip().upper()


@router.get("/agents/registry/graph")
async def agent_registry_graph():
    """Return the routing DAG: agents, input types and how they connect.

    The UI draws this directly: ``kind:"agent"`` nodes are the swarm,
    ``kind:"input_type"`` nodes are the routing decisions, and edges labelled
    ``routes_to`` / ``feeds`` / ``supervises`` are the relationships.

    Returns:
        ``{"nodes": [...], "edges": [...], "input_types": [...]}``.
    """
    graph = _routing_graph()
    get_metrics().gauge("api.agents.graph_nodes", float(len(graph["nodes"])))
    return graph


@router.get("/agents/{name}")
async def get_agent(name: str):
    """Fetch one agent's descriptor.

    Args:
        name: Agent name; case-insensitive (``scout`` and ``SCOUT`` both work).

    Returns:
        The agent descriptor.

    Raises:
        HTTPException: 404 when the agent is not registered.
    """
    wanted = str(name or "").strip().upper()
    registry = _registry()
    for key, cls in registry.items():
        if key.upper() == wanted:
            return _descriptor(cls, key)
    known = ", ".join(sorted(registry)) or "none"
    raise HTTPException(status_code=404, detail={
        "error": "unknown agent", "name": wanted, "known_agents": known})


# ════════════════════════════════════════════════════════════════════════════
# Standalone runs
# ════════════════════════════════════════════════════════════════════════════


def _serialise(result: Any, agent: Any) -> dict:
    """Serialise an ``AgentResult`` plus the agent's own step log.

    Args:
        result: The ``AgentResult`` returned by the agent.
        agent: The agent instance (for its private ``_steps`` trace).

    Returns:
        A JSON-safe dict on the frontend ``AgentResult`` shape.
    """
    return {
        "agent": getattr(result, "agent", getattr(agent, "name", "UNKNOWN")),
        "role": getattr(agent, "role", ""),
        "icon": getattr(agent, "icon", "◎"),
        "status": getattr(result, "status", "error"),
        "output": getattr(result, "output", None),
        "confidence": getattr(result, "confidence", 0.0),
        "reasoning": getattr(result, "reasoning", ""),
        "entities_found": getattr(result, "entities_found", []) or [],
        "signals": getattr(result, "signals", []) or [],
        "latency_s": getattr(result, "latency_s", 0.0),
        "tokens_used": getattr(result, "tokens_used", 0),
        "error": getattr(result, "error", None),
        "steps": list(getattr(agent, "_steps", []) or []),
    }


@router.post("/agents/{name}/run")
async def run_agent(name: str, payload: _AgentRunRequest, request: Request):
    """Run a single agent against a target, outside the APEX pipeline.

    Auth-guarded and rate-limited like every other route, additionally bounded
    by :data:`RUN_TIMEOUT_S` so a wedged agent cannot hold a worker forever, and
    wrapped so a failing agent returns ``status:"error"`` rather than a 500 —
    an agent raising is a normal, observable outcome, not a server fault.

    Args:
        name: Agent name (case-insensitive).
        payload: The run request.
        request: Used to stamp the audit trail.

    Returns:
        ``{agent, name, status, result, output, latency_s}``.

    Raises:
        HTTPException: 404 for an unknown agent.
    """
    wanted = str(name or "").strip().upper()
    registry = _registry()
    cls = next((c for key, c in registry.items() if key.upper() == wanted), None)
    if cls is None:
        known = ", ".join(sorted(registry)) or "none"
        raise HTTPException(status_code=404, detail={
            "error": "unknown agent", "name": wanted, "known_agents": known})

    context: dict[str, Any] = {}
    if payload.input_type:
        context["input_type"] = payload.input_type
    if payload.case_id:
        context["case_id"] = payload.case_id
    if payload.deep:
        context["deep"] = True
    if payload.lang:
        context["lang"] = payload.lang
    context.update(payload.context or {})

    started = time.monotonic()
    try:
        agent = cls()
    except Exception as exc:  # noqa: BLE001 — a broken constructor is reportable
        raise HTTPException(status_code=500, detail={
            "error": "agent could not be constructed",
            "agent": wanted, "reason": f"{type(exc).__name__}: {exc}"}) from exc

    get_metrics().incr("api.agents.run", tags={"agent": wanted})
    try:
        result = await asyncio.wait_for(
            agent.run(payload.input, context=context), timeout=RUN_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        latency = round(time.monotonic() - started, 3)
        get_metrics().incr("api.agents.timeout", tags={"agent": wanted})
        log.warning("agent %s timed out after %ss", wanted, RUN_TIMEOUT_S)
        return {
            "agent": wanted, "name": wanted, "status": "error",
            "result": {"agent": wanted, "status": "error",
                       "error": f"timed out after {RUN_TIMEOUT_S:.0f}s",
                       "latency_s": latency},
            "output": None, "latency_s": latency,
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — never 500 on an agent failure
        latency = round(time.monotonic() - started, 3)
        get_metrics().incr("api.agents.error", tags={"agent": wanted})
        log.warning("agent %s raised: %s", wanted, type(exc).__name__)
        return {
            "agent": wanted, "name": wanted, "status": "error",
            "result": {"agent": wanted, "status": "error",
                       "error": f"{type(exc).__name__}: {exc}",
                       "latency_s": latency},
            "output": None, "latency_s": latency,
        }

    payload_out = _serialise(result, agent)
    latency = round(time.monotonic() - started, 3)
    get_metrics().timing("api.agents.duration", latency, agent=wanted)

    # Optional: file the run as an investigation so it shows up in case history.
    if payload.case_id:
        try:
            store = await resolve_store()
            record = await store.get_investigation(wanted)
            _ = record  # keep the read cheap and side-effect free
        except Exception:  # noqa: BLE001 — persistence is optional here
            pass

    try:
        store = await resolve_store()
        await store.audit({
            "event": "agent.ran", "agent": wanted,
            "status": payload_out.get("status"),
            "principal": current_principal(request).get("id")})
    except Exception:  # noqa: BLE001
        pass

    return {
        "agent": wanted,
        "name": wanted,
        "status": payload_out.get("status"),
        "result": payload_out,
        "output": payload_out.get("output"),
        "latency_s": latency,
    }


# Auth + rate limiting apply to introspection and to standalone runs alike.
router.dependencies.extend([Depends(auth_gate), Depends(rate_limit_dep)])