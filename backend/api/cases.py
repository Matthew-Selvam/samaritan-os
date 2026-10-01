"""cases.py — investigation case management.

Owned by WS-API. New surface (none of it existed in ``main.py``, whose docstring
advertised ``GET /api/cases`` but never implemented it):

    GET    /api/cases                  list, newest first, paginated
    POST   /api/cases                  create
    GET    /api/cases/{case_id}        fetch
    PATCH  /api/cases/{case_id}        partial update
    DELETE /api/cases/{case_id}        delete the case and its investigations
    GET    /api/cases/{case_id}/stats  aggregate counters
    GET    /api/cases/{case_id}/entities   every entity seen across the case
    GET    /api/cases/{case_id}/timeline   merged chronological timeline
    POST   /api/cases/{case_id}/export     in-memory ZIP bundle

A *case* is the unit an analyst files an investigation under (``case_id`` on
every investigation record). Entities and timelines are derived on read from the
case's investigations rather than stored twice, so they can never drift from the
reports they came from.

Pagination: ``GET /api/cases`` returns the ``{items, total, limit, offset}``
envelope the frontend ``Paged<T>`` type documents. The frontend's
``unwrapList`` also accepts a bare array, but the envelope is the documented
shape and carries the total.

Export is built entirely in memory with :mod:`zipfile` against a
:class:`io.BytesIO` buffer — no temp files, no cleanup window, no disk pressure.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from api.ops import (
    guarded_router,
    auth_gate,
    build_zip,
    clamp_limit,
    clamp_offset,
    current_principal,
    paged,
    paginate,
    rate_limit_dep,
    resolve_store,
    _maybe_import,
)
from observability import get_logger, get_metrics

log = get_logger("signal-os.api.cases")

router = guarded_router(prefix="/api/cases", tags=["cases"])

#: Cap on rows pulled from the store when deriving entities/timeline for a case,
#: so a case with thousands of investigations cannot produce an unbounded export.
DERIVE_WINDOW = 500

#: Rows fetched to serve a paginated case listing. Bounded so the total is stable
#: across pages; the store itself is capped at config.STORE_MAX_ROWS.
MAX_LIST_WINDOW = 1000

#: Longest accepted case name. Enforced at the handler boundary (see
#: `_enforce_name_bound`) because `schemas.CaseCreate` inherits a model-wide
#: `str_max_length=4096` from `RequestModel`, and in pydantic a config-level
#: string bound *replaces* the per-field `max_length` — so the field's own
#: 256-char limit was silently not being applied.
MAX_CASE_NAME_CHARS = 256


def _now_iso() -> str:
    """Return the current UTC instant as ISO-8601 with a ``Z`` suffix.

    Returns:
        An ISO-8601 timestamp.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ════════════════════════════════════════════════════════════════════════════
# Request models
# ════════════════════════════════════════════════════════════════════════════
#
# `CaseCreate` / `CaseUpdate` live in `schemas.py` per CONTRACTS.md §8 once WS-SEC
# has landed; the local definitions below are field-identical and are used until
# then. Exactly one definition is ever live per process.


def _pick_schema(local: type, name: str) -> type:
    """Return ``schemas.<name>`` when WS-SEC defines it, else ``local``.

    Args:
        local: The locally-defined equivalent model.
        name: Class name to look up in ``schemas``.

    Returns:
        The model class this module should use.
    """
    schemas = _maybe_import("schemas")
    candidate = getattr(schemas, name, None) if schemas else None
    if isinstance(candidate, type) and issubclass(candidate, BaseModel):
        return candidate
    return local


class _CaseCreate(BaseModel):
    """Create a case.

    Attributes:
        name: Human label for the case.
        target: Optional primary target this case tracks.
        tags: Free-form labels for filtering.
        notes: Free-text analyst notes.
        case_id: Supply an id to create the case with a known key; generated
            when omitted.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_CASE_NAME_CHARS)
    target: Optional[str] = Field(default=None, max_length=2048)
    tags: list[str] = Field(default_factory=list, max_length=32)
    notes: Optional[str] = Field(default=None, max_length=8192)
    case_id: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class _CaseUpdate(BaseModel):
    """Partially update a case. Every field is optional; ``None`` means "leave
    it alone", so an explicit ``null`` cannot erase a value by accident."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=256)
    target: Optional[str] = Field(default=None, max_length=2048)
    tags: Optional[list[str]] = Field(default=None, max_length=32)
    notes: Optional[str] = Field(default=None, max_length=8192)


CaseCreate = _pick_schema(_CaseCreate, "CaseCreate")
CaseUpdate = _pick_schema(_CaseUpdate, "CaseUpdate")


# ════════════════════════════════════════════════════════════════════════════
# Derivation helpers
# ════════════════════════════════════════════════════════════════════════════


def _reports_of(rows: list[dict]) -> list[dict]:
    """Extract the report dict from each investigation row.

    Args:
        rows: Investigation records.

    Returns:
        The subset that carries a dict-shaped ``report``.
    """
    out: list[dict] = []
    for row in rows:
        report = row.get("report")
        if isinstance(report, dict):
            out.append(report)
    return out


def _entity_key(entity: dict) -> str:
    """Compute the canonical, deterministic entity id.

    CONTRACTS.md §3 pins the format to ``f"{type}:{normalized_value}"``; an
    entity that already carries an ``id`` keeps it.

    Args:
        entity: The entity dict.

    Returns:
        A stable dedup key.
    """
    existing = str(entity.get("id") or "").strip()
    if existing:
        return existing
    etype = str(entity.get("type") or "other").strip().lower()
    value = str(entity.get("value") or entity.get("label") or "").strip().lower()
    return f"{etype}:{value}"


def _collect_entities(reports: list[dict]) -> list[dict]:
    """Deduplicate entities across a set of reports, keeping the best confidence.

    Args:
        reports: Report dicts.

    Returns:
        Entity dicts sorted by descending confidence, each with a canonical
        ``id``, and provenance fields merged across duplicates.
    """
    merged: dict[str, dict] = {}
    for report in reports:
        for raw in report.get("entities") or []:
            if not isinstance(raw, dict):
                continue
            entity = dict(raw)
            key = _entity_key(entity)
            try:
                confidence = float(entity.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            existing = merged.get(key)
            if existing is None:
                entity["id"] = key
                entity.setdefault("confidence", confidence)
                merged[key] = entity
                continue
            # Keep the strongest claim, but remember every agent that saw it.
            try:
                existing_conf = float(existing.get("confidence") or 0.0)
            except (TypeError, ValueError):
                existing_conf = 0.0
            if confidence > existing_conf:
                for field in ("label", "value", "source", "attrs", "first_seen"):
                    if entity.get(field):
                        existing[field] = entity[field]
                existing["confidence"] = confidence
            sources = set()
            for value in (existing.get("source"), entity.get("source")):
                for part in str(value or "").split(","):
                    if part.strip():
                        sources.add(part.strip())
            if len(sources) > 1:
                existing["source"] = ", ".join(sorted(sources))
    return sorted(
        merged.values(),
        key=lambda e: (-float(e.get("confidence") or 0.0), str(e.get("id"))),
    )


def _collect_signals(reports: list[dict]) -> list[dict]:
    """Deduplicate signals across a set of reports.

    Signals are keyed by ``(type, value, url)`` because the same breach can be
    observed by several agents under one type.

    Args:
        reports: Report dicts.

    Returns:
        Signal dicts with ``confidence`` always present (CONTRACTS.md §3).
    """
    merged: dict[str, dict] = {}
    for report in reports:
        for raw in report.get("signals") or []:
            if not isinstance(raw, dict):
                continue
            signal = dict(raw)
            key = "|".join([
                str(signal.get("type") or ""),
                json.dumps(signal.get("value"), sort_keys=True, default=str),
                str(signal.get("url") or ""),
            ])
            try:
                confidence = float(signal.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            signal["confidence"] = confidence
            existing = merged.get(key)
            if existing is None or confidence > float(existing.get("confidence") or 0.0):
                merged[key] = signal
    return sorted(
        merged.values(),
        key=lambda s: (-float(s.get("confidence") or 0.0), str(s.get("type") or "")),
    )


def _collect_timeline(reports: list[dict]) -> list[dict]:
    """Merge per-report timelines into one chronological list.

    Events keep the frontend ``TimelineEvent`` shape (id, date, label, ...) and
    are ordered by ``ts`` then ``date``, with undated events last. Events missing
    an ``id`` are given a deterministic one derived from their position so the
    frontend can key on them.

    Args:
        reports: Report dicts.

    Returns:
        Chronologically ordered timeline events.
    """
    events: list[dict] = []
    for report in reports:
        for raw in report.get("timeline") or []:
            if not isinstance(raw, dict):
                continue
            event = dict(raw)
            event.setdefault("date", event.get("ts") or "")
            event.setdefault("label", event.get("description") or "event")
            event.setdefault("id", "evt:" + str(_entity_key(
                {"type": "note", "value": f"{event.get('source', '')}|{event.get('label', '')}|{event.get('date', '')}"}
            )))
            events.append(event)

    def _sort_key(event: dict) -> tuple:
        return (0, str(event.get("ts") or ""), str(event.get("date") or "")) \
            if (event.get("ts") or event.get("date")) else (1, "", "")

    return sorted(events, key=_sort_key)


def _collect_graph(reports: list[dict]) -> dict:
    """Merge per-report knowledge graphs into one.

    Args:
        reports: Report dicts.

    Returns:
        ``{"nodes", "edges", "clusters"}`` with duplicate nodes/edges collapsed
        and edges restricted to nodes that exist in the merged set.
    """
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}
    clusters: dict[str, dict] = {}
    for report in reports:
        graph = report.get("graph")
        if not isinstance(graph, dict):
            continue
        for raw in graph.get("nodes") or []:
            if not isinstance(raw, dict):
                continue
            node = dict(raw)
            node_id = str(node.get("id") or "")
            if not node_id:
                continue
            nodes.setdefault(node_id, node)
        for raw in graph.get("edges") or []:
            if not isinstance(raw, dict):
                continue
            edge = dict(raw)
            source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
            if not source or not target:
                continue
            edges.setdefault((source, target, str(edge.get("type") or "")), edge)
        for raw in graph.get("clusters") or []:
            if not isinstance(raw, dict):
                continue
            cluster = dict(raw)
            cluster_id = str(cluster.get("id") or "")
            if cluster_id:
                clusters.setdefault(cluster_id, cluster)
    live = [e for key, e in edges.items() if key[0] in nodes and key[1] in nodes]
    return {
        "nodes": list(nodes.values()),
        "edges": live,
        "clusters": list(clusters.values()),
    }


async def _case_row(case_id: str) -> dict:
    """Fetch a case or raise a 404.

    Args:
        case_id: The case identifier.

    Returns:
        The stored case record.

    Raises:
        HTTPException: 404 when the case does not exist.
    """
    store = await resolve_store()
    case = await store.get_case(str(case_id)[:64])
    if not case:
        raise HTTPException(status_code=404, detail={"error": "case not found"})
    return case


async def _case_reports(case_id: str) -> tuple[list[dict], list[dict]]:
    """Load a case's investigations and their reports.

    Args:
        case_id: The case identifier.

    Returns:
        ``(investigation_rows, report_dicts)``.
    """
    store = await resolve_store()
    rows = await store.list_investigations(case_id=case_id, limit=DERIVE_WINDOW)
    return rows, _reports_of(rows)


def _stats_payload(case_id: str, case: dict, rows: list[dict], reports: list[dict]) -> dict:
    """Build the ``/stats`` payload.

    Combines the store's own aggregates (which are authoritative for counters)
    with values derived from the reports, mapped onto the frontend
    ``CaseStats`` field names: ``investigation_count``, ``entity_count``,
    ``signal_count``, ``agent_count``, ``first_seen``, ``last_seen``,
    ``entity_types``.

    Args:
        case_id: The case identifier.
        case: The case record.
        rows: The case's investigations.
        reports: The case's reports.

    Returns:
        The statistics dict.
    """
    entities = _collect_entities(reports)
    signals = _collect_signals(reports)
    agent_names: set[str] = set()
    for row in rows:
        for agent in row.get("agents") or []:
            if isinstance(agent, dict) and agent.get("agent"):
                agent_names.add(str(agent["agent"]))

    by_type: dict[str, int] = {}
    for entity in entities:
        key = str(entity.get("type") or "other")
        by_type[key] = by_type.get(key, 0) + 1

    stamps = [str(r.get("created_at") or "") for r in rows if r.get("created_at")]
    stamps += [str(r.get("started_at") or "") for r in rows if r.get("started_at")]
    stamps = sorted(s for s in stamps if s)

    return {
        "case_id": case_id,
        "name": case.get("name"),
        "investigation_count": len(rows),
        "entity_count": len(entities),
        "signal_count": len(signals),
        "agent_count": len(agent_names),
        "agents": sorted(agent_names),
        "entity_types": by_type,
        "first_seen": stamps[0] if stamps else None,
        "last_seen": stamps[-1] if stamps else None,
        "tags": list(case.get("tags") or []),
    }


def _enforce_name_bound(name: Any) -> None:
    """Reject a case name longer than :data:`MAX_CASE_NAME_CHARS`.

    The effective ``CaseCreate`` comes from ``schemas.py``, whose
    ``RequestModel`` sets a model-wide ``str_max_length=4096``. Pydantic treats a
    config-level string bound as *replacing* the per-field ``max_length``, so the
    declared 256-char limit on ``name`` never actually applied and a 4 KB
    "case name" was accepted. Re-checking here keeps the documented bound real
    without editing another workstream's schema.

    Args:
        name: The submitted case name.

    Raises:
        HTTPException: 422 when the name exceeds the bound.
    """
    if isinstance(name, str) and len(name) > MAX_CASE_NAME_CHARS:
        raise HTTPException(status_code=422, detail=[{
            "loc": ["body", "name"],
            "msg": f"String should have at most {MAX_CASE_NAME_CHARS} characters",
            "type": "string_too_long",
        }])


# ════════════════════════════════════════════════════════════════════════════
# Routes
# ════════════════════════════════════════════════════════════════════════════


@router.get("")
@router.get("/", include_in_schema=False)
async def list_cases(limit: int = 50, offset: int = 0):
    """List cases, newest first.

    Registered on both ``""`` and ``"/"``: FastAPI redirects ``/api/cases`` to
    ``/api/cases/`` for a slash-terminated route, and that 307 breaks the
    frontend client (``fetch`` follows it for GET but POST bodies and
    ``X-API-Key`` headers are not guaranteed to survive the redirect). Binding
    the bare path explicitly is what makes both spellings work.

    Args:
        limit: Page size (capped at 200).
        offset: Cases to skip.

    Returns:
        ``{"items": [...], "total": n, "limit": l, "offset": o}``; each item is
        the case record plus ``investigation_count``.
    """
    size = clamp_limit(limit)
    start = clamp_offset(offset)
    # `total` has to describe the whole collection, not the page window. The
    # store's list API is limit-only (no COUNT), so fetching `size + start + 1`
    # and calling `len()` the total under-reports by up to `start + 1` — a
    # client paginating to the last page would see the count shrink as it
    # advanced. Fetch one bounded window instead and page from that; the store
    # is already capped (STORE_MAX_ROWS), so this stays bounded.
    store = await resolve_store()
    rows = await store.list_cases(limit=MAX_LIST_WINDOW)
    return paged(rows, size, start)


@router.post("")
@router.post("/", include_in_schema=False)
async def create_case(payload: CaseCreate, request: Request):
    """Create (or upsert) a case.

    Registered on both ``""`` and ``"/"`` — see :func:`list_cases` for why the
    bare path matters (``POST /api/cases`` must not 307-redirect).

    Args:
        payload: The case to create.
        request: Used to stamp the audit trail.

    Returns:
        The stored case record, including its ``case_id``.

    Raises:
        HTTPException: 422 when a field exceeds its documented bound.
    """
    _enforce_name_bound(payload.name)
    case_id = payload.case_id or str(uuid.uuid4())[:8]
    record = {
        "case_id": case_id,
        "name": payload.name,
        "target": payload.target,
        "tags": list(payload.tags or []),
        "notes": payload.notes,
        "created_at": _now_iso(),
    }
    store = await resolve_store()
    case = await store.upsert_case(record)
    get_metrics().incr("api.cases.created")
    try:
        await store.audit({
            "event": "case.created", "case_id": case_id,
            "principal": current_principal(request).get("id")})
    except Exception:  # noqa: BLE001 — auditing is best-effort
        pass
    return case


@router.get("/{case_id}")
async def get_case(case_id: str):
    """Fetch one case.

    Args:
        case_id: The case identifier.

    Returns:
        The case record with ``investigation_count`` added.

    Raises:
        HTTPException: 404 when the case does not exist.
    """
    case = await _case_row(case_id)
    store = await resolve_store()
    rows = await store.list_investigations(case_id=case_id, limit=DERIVE_WINDOW)
    case["investigation_count"] = len(rows)
    return case


@router.patch("/{case_id}")
async def update_case(case_id: str, payload: CaseUpdate, request: Request):
    """Partially update a case.

    Only fields explicitly present in the body are written; ``None`` means
    "leave unchanged".

    Args:
        case_id: The case identifier.
        payload: Fields to change.
        request: Used to stamp the audit trail.

    Returns:
        The updated case record.

    Raises:
        HTTPException: 404 when the case does not exist.
    """
    case = await _case_row(case_id)
    patch = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not patch:
        return case
    patch["case_id"] = case["case_id"]
    store = await resolve_store()
    updated = await store.upsert_case(patch)
    get_metrics().incr("api.cases.updated")
    try:
        await store.audit({
            "event": "case.updated", "case_id": case_id,
            "fields": sorted(patch), "principal": current_principal(request).get("id")})
    except Exception:  # noqa: BLE001
        pass
    return updated


@router.delete("/{case_id}")
async def delete_case(case_id: str, request: Request):
    """Delete a case together with every investigation filed under it.

    Args:
        case_id: The case identifier.
        request: Used to stamp the audit trail.

    Returns:
        ``{"deleted": true, "case_id": ...}``.

    Raises:
        HTTPException: 404 when the case does not exist.
    """
    await _case_row(case_id)
    store = await resolve_store()
    deleted = await store.delete_case(case_id)
    get_metrics().incr("api.cases.deleted")
    try:
        await store.audit({
            "event": "case.deleted", "case_id": case_id,
            "deleted": deleted, "principal": current_principal(request).get("id")})
    except Exception:  # noqa: BLE001
        pass
    return {"deleted": True, "case_id": case_id}


@router.get("/{case_id}/stats")
async def case_stats(case_id: str):
    """Aggregate statistics for one case.

    Args:
        case_id: The case identifier.

    Returns:
        ``{case_id, name, investigation_count, entity_count, signal_count,
        agent_count, agents, entity_types, first_seen, last_seen, tags}``.

    Raises:
        HTTPException: 404 when the case does not exist.
    """
    case = await _case_row(case_id)
    rows, reports = await _case_reports(case_id)
    return _stats_payload(case_id, case, rows, reports)


@router.get("/{case_id}/entities")
async def case_entities(case_id: str, limit: int = 200, offset: int = 0,
                        type: Optional[str] = None):
    """Every entity observed across a case's investigations.

    Args:
        case_id: The case identifier.
        limit: Page size (capped at 500).
        offset: Entities to skip.
        type: Optional entity-type filter.

    Returns:
        A **bare JSON array** of deduplicated entities, matching the frontend's
        ``getCaseEntities`` which reads either an array or ``{entities: [...]}``.

    Raises:
        HTTPException: 404 when the case does not exist.
    """
    await _case_row(case_id)
    _rows, reports = await _case_reports(case_id)
    entities = _collect_entities(reports)
    if type:
        wanted = str(type).strip().lower()
        entities = [e for e in entities if str(e.get("type") or "").lower() == wanted]
    return paginate(entities, min(clamp_limit(limit, maximum=500), 500), clamp_offset(offset))


@router.get("/{case_id}/timeline")
async def case_timeline(case_id: str, limit: int = 500, offset: int = 0):
    """Merged timeline across a case's investigations.

    Args:
        case_id: The case identifier.
        limit: Page size (capped at 500).
        offset: Events to skip.

    Returns:
        A **bare JSON array** of chronologically ordered ``TimelineEvent`` dicts,
        matching the frontend's ``getCaseTimeline``.

    Raises:
        HTTPException: 404 when the case does not exist.
    """
    await _case_row(case_id)
    _rows, reports = await _case_reports(case_id)
    events = _collect_timeline(reports)
    return paginate(events, min(clamp_limit(limit, maximum=500), 500), clamp_offset(offset))


@router.post("/{case_id}/export", response_class=Response)
async def export_case(case_id: str, request: Request, include_raw: bool = False):
    """Export a case as an in-memory ZIP bundle.

    The archive is assembled in a :class:`io.BytesIO` buffer — no temp files are
    created and nothing is left on disk. Contents:

    * ``case.json`` — the case record plus its statistics
    * ``entities.json`` — every deduplicated entity
    * ``signals.json`` — every deduplicated signal
    * ``timeline.json`` — the merged timeline
    * ``graph.json`` — the merged knowledge graph
    * ``investigations.json`` — the investigation index
    * ``reports/<inv_id>.md`` — one markdown report per investigation
    * ``README.md`` — a human-readable manifest

    Args:
        case_id: The case identifier.
        request: Used to stamp the audit trail.
        include_raw: Include each investigation's full stored record (which
            embeds every agent result) rather than just its index fields.

    Returns:
        A ``application/zip`` response.

    Raises:
        HTTPException: 404 when the case does not exist.
    """
    case = await _case_row(case_id)
    rows, reports = await _case_reports(case_id)
    reports_by_inv = {
        str(row.get("inv_id")): row.get("report")
        for row in rows if isinstance(row.get("report"), dict)
    }
    entities = _collect_entities(reports)
    signals = _collect_signals(reports)
    timeline = _collect_timeline(reports)
    graph = _collect_graph(reports)
    stats = _stats_payload(case_id, case, rows, reports)

    index = [
        {
            "inv_id": row.get("inv_id"),
            "input": row.get("input"),
            "input_type": row.get("input_type"),
            "status": row.get("status"),
            "created_at": row.get("created_at"),
            "ended_at": row.get("ended_at"),
            "confidence": row.get("confidence"),
        }
        for row in rows
    ]

    entries: dict[str, str | bytes] = {
        "case.json": json.dumps({"case": case, "stats": stats}, indent=2, default=str),
        "entities.json": json.dumps(entities, indent=2, default=str),
        "signals.json": json.dumps(signals, indent=2, default=str),
        "timeline.json": json.dumps(timeline, indent=2, default=str),
        "graph.json": json.dumps(graph, indent=2, default=str),
        "investigations.json": json.dumps(
            rows if include_raw else index, indent=2, default=str),
    }

    manifest = [
        f"# Case {case.get('name') or case_id}",
        "",
        f"- case_id: `{case_id}`",
        f"- exported_at: {_now_iso()}",
        f"- investigations: {len(rows)}",
        f"- entities: {len(entities)}",
        f"- signals: {len(signals)}",
        f"- timeline events: {len(timeline)}",
        f"- graph nodes: {len(graph['nodes'])}, edges: {len(graph['edges'])}",
        "",
        "## Contents",
        "",
        "| file | what |",
        "|---|---|",
        "| `case.json` | case record + statistics |",
        "| `investigations.json` | investigation index |",
        "| `entities.json` | deduplicated entities |",
        "| `signals.json` | deduplicated signals |",
        "| `timeline.json` | merged timeline |",
        "| `graph.json` | merged knowledge graph |",
        "| `reports/*.md` | one markdown report per investigation |",
        "",
        "## Investigations",
        "",
    ]
    for item in index:
        manifest.append(f"- `{item['inv_id']}` — {item.get('input_type')} — "
                        f"{item.get('status')} — {item.get('input')}")
    entries["README.md"] = "\n".join(manifest) + "\n"

    for inv_id, report in reports_by_inv.items():
        markdown = (report or {}).get("markdown")
        body = markdown or _synthesise_markdown(inv_id, report or {}, case_id)
        entries[f"reports/{inv_id}.md"] = body

    payload = build_zip(entries)
    get_metrics().incr("api.cases.exported", tags={"entries": str(len(entries))})
    try:
        store = await resolve_store()
        await store.audit({
            "event": "case.exported", "case_id": case_id,
            "bytes": len(payload), "principal": current_principal(request).get("id")})
    except Exception:  # noqa: BLE001
        pass
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="case-{case_id}.zip"',
            "X-Export-Entries": str(len(entries)),
            "X-Export-Bytes": str(len(payload)),
        },
    )


def _synthesise_markdown(inv_id: str, report: dict, case_id: str) -> str:
    """Render a minimal markdown report for a run that produced no markdown.

    QUILL normally supplies the prose; this keeps every exported investigation
    readable when it errored, was skipped, or ran without the report agent.

    Args:
        inv_id: The investigation id.
        report: The stored report dict.
        case_id: The owning case id.

    Returns:
        A markdown document.
    """
    lines = [
        f"# Investigation {inv_id}",
        "",
        f"- case: `{case_id}`",
        f"- input type: {report.get('input_type') or 'unknown'}",
        f"- confidence: {report.get('confidence') or 0:.0%}"
        if isinstance(report.get("confidence"), (int, float)) else "- confidence: n/a",
        f"- latency: {report.get('latency_s') or 0:.1f}s",
        "",
        "## Summary",
        "",
        report.get("summary") or "_(no summary recorded)_",
        "",
        "## Entities",
        "",
    ]
    entities = report.get("entities") or []
    if entities:
        lines += ["| entity | type | confidence |", "|---|---|---|"]
        for entity in entities[:200]:
            if isinstance(entity, dict):
                lines.append(
                    f"| {entity.get('label') or entity.get('value') or entity.get('id')} "
                    f"| {entity.get('type')} | {entity.get('confidence', 0)} |")
    else:
        lines.append("_No entities recorded._")
    lines += ["", "## Signals", ""]
    signals = report.get("signals") or []
    if signals:
        for signal in signals[:200]:
            if isinstance(signal, dict):
                lines.append(f"- **{signal.get('type')}** — {signal.get('value')} "
                             f"({signal.get('confidence', 0)})")
    else:
        lines.append("_No signals recorded._")
    lines += ["", "## Timeline", ""]
    events = report.get("timeline") or []
    if events:
        for event in events[:200]:
            if isinstance(event, dict):
                lines.append(f"- {event.get('date') or event.get('ts') or 'undated'} — "
                             f"{event.get('label')}")
    else:
        lines.append("_No timeline events recorded._")
    return "\n".join(lines) + "\n"

