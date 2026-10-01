"""reports.py — report rendering and artefact export for one investigation.

Owned by WS-API. New surface:

    GET /api/investigate/{inv_id}/report?format=markdown|pdf|json
    GET /api/investigate/{inv_id}/export?format=markdown|pdf|json

``/report`` is what the UI renders; ``/export`` is the same document as a
downloadable artefact. Both read from the stored investigation (and the
persisted report row when one exists), so they work for runs started by
``/api/investigate``, ``/api/investigate-sync``, ``/api/photo-search`` or
``/api/name-search`` alike.

PDF is rendered by :func:`api.ops.render_pdf_bytes`, a dependency-free PDF 1.4
writer, because the platform must keep working with zero installed packages;
``config.FEATURE_PDF_EXPORT=false`` disables the format behind a 400 rather
than silently downgrading the caller to markdown.

Neither endpoint leaks the raw input for investigations that have not finished:
an unknown id is a 404 and an unfinished run is a 409, so a client polling for
a report cannot mistake a half-written pipeline for an empty finding.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from api.ops import (
    guarded_router,
    auth_gate,
    build_zip,
    current_principal,
    render_pdf_bytes,
    rate_limit_dep,
    resolve_store,
    _maybe_import,
)
from observability import get_logger, get_metrics

log = get_logger("signal-os.api.reports")

router = guarded_router(prefix="/api", tags=["reports"])

#: Formats accepted by ``/report`` and ``/export``.
REPORT_FORMATS = ("markdown", "pdf", "json")

#: Format used when the caller does not specify one.
DEFAULT_FORMAT = "markdown"

#: Longest slice of an input echoed into an export filename.
_FILENAME_INPUT_CHARS = 40


def _now_iso() -> str:
    """Return the current UTC instant as ISO-8601 with a ``Z`` suffix.

    Returns:
        An ISO-8601 timestamp.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_format(fmt: Optional[str]) -> str:
    """Validate a ``format`` query parameter.

    Args:
        fmt: The requested format; ``None`` selects the default.

    Returns:
        One of :data:`REPORT_FORMATS`.

    Raises:
        HTTPException: 400 for an unsupported format, or 501 when PDF export is
            switched off by configuration.
    """
    value = str(fmt or DEFAULT_FORMAT).strip().lower()
    if value not in REPORT_FORMATS:
        raise HTTPException(status_code=400, detail={
            "error": "unsupported format", "requested": value,
            "supported": list(REPORT_FORMATS)})
    if value == "pdf":
        config = _maybe_import("config")
        enabled = True
        if config is not None and hasattr(config, "FEATURE_PDF_EXPORT"):
            enabled = bool(getattr(config, "FEATURE_PDF_EXPORT", True))
        if not enabled:
            raise HTTPException(status_code=501, detail={
                "error": "pdf export disabled", "feature": "pdf_export"})
    return value


def _slug(text: str) -> str:
    """Reduce arbitrary text to a filesystem-safe slug.

    Args:
        text: Source text (usually part of an investigation input).

    Returns:
        A short lowercase slug, or ``"report"`` when nothing survives.
    """
    keep = [c.lower() if c.isalnum() else "-" for c in str(text or "")[:_FILENAME_INPUT_CHARS]]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:48] or "report"


async def _load(inv_id: str) -> tuple[dict, dict]:
    """Load an investigation and its report.

    Args:
        inv_id: The investigation identifier.

    Returns:
        ``(record, report)``. ``report`` may be ``{}`` when the run stored none.

    Raises:
        HTTPException: 404 when the investigation is unknown.
    """
    inv_id = str(inv_id or "")[:64]
    store = await resolve_store()
    record = await store.get_investigation(inv_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": "not found"})
    report = record.get("report")
    if not isinstance(report, dict):
        # Fall back to a previously persisted report row before giving up.
        getter = getattr(store, "get_latest_report", None)
        if callable(getter):
            try:
                stored = await getter(inv_id)
                report = stored.get("meta", {}) if isinstance(stored, dict) else {}
            except Exception:  # noqa: BLE001 — persistence is best-effort
                report = {}
        else:
            report = {}
    return record, report


def _guard_finished(record: dict) -> None:
    """Refuse to render a report for a run that has not finished.

    Args:
        record: The stored investigation.

    Raises:
        HTTPException: 409 while the investigation is queued or running.
    """
    status = str(record.get("status") or "").lower()
    if status in ("queued", "running", "routing"):
        raise HTTPException(status_code=409, detail={
            "error": "investigation still running", "inv_id": record.get("inv_id"),
            "status": status})


def _markdown_for(record: dict, report: dict) -> str:
    """Return the markdown body for an investigation.

    Uses QUILL's rendered markdown when present, otherwise the row persisted by
    ``store.save_report``, otherwise a deterministic rendering built from the
    stored entities/signals/timeline so an export is never empty just because the
    prose agent was skipped.

    Args:
        record: The stored investigation.
        report: Its report dict.

    Returns:
        A markdown document.
    """
    markdown = (report or {}).get("markdown")
    if isinstance(markdown, str) and markdown.strip():
        return markdown

    header = [
        f"# Investigation {record.get('inv_id')}",
        "",
        f"- case: `{record.get('case_id') or '-'}`",
        f"- input: `{str(record.get('input') or '')[:200]}`",
        f"- input type: {record.get('input_type') or 'unknown'}",
        f"- status: {record.get('status')}",
        f"- started: {record.get('started_at') or '-'}",
        f"- ended: {record.get('ended_at') or '-'}",
    ]
    confidence = report.get("confidence") if isinstance(report, dict) else None
    if isinstance(confidence, (int, float)):
        header.append(f"- confidence: {confidence:.0%}")
    latency = report.get("latency_s") if isinstance(report, dict) else None
    if isinstance(latency, (int, float)):
        header.append(f"- latency: {latency:.1f}s")

    agents = record.get("agents") or []
    body = header + ["", "## Summary", "",
                     (report.get("summary") if isinstance(report, dict) else None)
                     or "_(no summary recorded)_", "", "## Agents", ""]
    if agents:
        body.append("| agent | status | confidence | latency |")
        body.append("|---|---|---|---|")
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            body.append(
                f"| {agent.get('agent')} | {agent.get('status')} "
                f"| {agent.get('confidence', 0)} | {agent.get('latency_s', 0)}s |")
            if agent.get("error"):
                body.append(f"| | ⚠ {str(agent['error'])[:120]} | | |")
    else:
        body.append("_No agent results recorded._")

    for title, key, renderer in (
        ("Entities", "entities", _render_entities),
        ("Signals", "signals", _render_signals),
        ("Timeline", "timeline", _render_timeline),
    ):
        body += ["", f"## {title}", ""]
        body += renderer(report.get(key) or [] if isinstance(report, dict) else [])

    graph = report.get("graph") if isinstance(report, dict) else None
    body += ["", "## Knowledge graph", ""]
    if isinstance(graph, dict) and (graph.get("nodes") or graph.get("edges")):
        body.append(f"- nodes: {len(graph.get('nodes') or [])}")
        body.append(f"- edges: {len(graph.get('edges') or [])}")
    else:
        body.append("_No graph produced._")

    return "\n".join(body) + "\n"


def _render_entities(entities: list) -> list[str]:
    """Render entities as a markdown table.

    Args:
        entities: Entity dicts.

    Returns:
        Markdown lines.
    """
    if not entities:
        return ["_No entities recorded._"]
    lines = ["| entity | type | confidence | source |", "|---|---|---|---|"]
    for entity in entities[:500]:
        if not isinstance(entity, dict):
            continue
        label = entity.get("label") or entity.get("value") or entity.get("id")
        lines.append(f"| {label} | {entity.get('type')} "
                     f"| {entity.get('confidence', 0)} | {entity.get('source', '-')} |")
    return lines


def _render_signals(signals: list) -> list[str]:
    """Render signals as a markdown list.

    Args:
        signals: Signal dicts.

    Returns:
        Markdown lines.
    """
    if not signals:
        return ["_No signals recorded._"]
    lines = []
    for signal in signals[:500]:
        if not isinstance(signal, dict):
            continue
        value = signal.get("value")
        rendered = value if isinstance(value, (str, int, float)) else json.dumps(
            value, default=str)[:160]
        link = f" ([source]({signal['url']}))" if signal.get("url") else ""
        lines.append(f"- **{signal.get('type')}** — {rendered} "
                     f"({signal.get('confidence', 0)}){link}")
    return lines


def _render_timeline(events: list) -> list[str]:
    """Render timeline events as a markdown list.

    Args:
        events: Timeline event dicts.

    Returns:
        Markdown lines.
    """
    if not events:
        return ["_No timeline events recorded._"]
    lines = []
    for event in events[:500]:
        if not isinstance(event, dict):
            continue
        stamp = event.get("date") or event.get("ts") or "undated"
        lines.append(f"- {stamp} — {event.get('label') or event.get('description') or ''}")
    return lines


def _json_for(record: dict, report: dict) -> dict:
    """Build the machine-readable report payload.

    Args:
        record: The stored investigation.
        report: Its report dict.

    Returns:
        ``{inv_id, case_id, format, generated_at, input, input_type, status,
        confidence, latency_s, agents, report}``.
    """
    return {
        "inv_id": record.get("inv_id"),
        "case_id": record.get("case_id"),
        "format": "json",
        "generated_at": _now_iso(),
        "input": record.get("input"),
        "input_type": record.get("input_type"),
        "status": record.get("status"),
        "started_at": record.get("started_at"),
        "ended_at": record.get("ended_at"),
        "confidence": report.get("confidence"),
        "latency_s": report.get("latency_s"),
        "agents": record.get("agents") or [],
        "steps": record.get("steps") or [],
        "report": report,
    }


# ════════════════════════════════════════════════════════════════════════════
# Routes
# ════════════════════════════════════════════════════════════════════════════


@router.get("/investigate/{inv_id}/report")
async def get_report(inv_id: str, format: Optional[str] = Query(default=None)):
    """Render one investigation's report.

    Args:
        inv_id: The investigation identifier.
        format: ``markdown`` (default), ``pdf`` or ``json``.

    Returns:
        ``markdown`` -> a ``text/markdown`` body; ``json`` -> the report
        payload; ``pdf`` -> an ``application/pdf`` download.

    Raises:
        HTTPException: 400 (bad format), 404 (unknown investigation), 409 (still
            running), 501 (pdf export disabled).
    """
    fmt = _validate_format(format)
    record, report = await _load(inv_id)
    _guard_finished(record)

    if fmt == "json":
        get_metrics().incr("api.reports.rendered", tags={"format": fmt})
        return _json_for(record, report)

    markdown = _markdown_for(record, report)
    filename = f"signal-os-{record.get('inv_id')}-{_slug(record.get('input'))}.md"

    if fmt == "markdown":
        get_metrics().incr("api.reports.rendered", tags={"format": fmt})
        return Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    pdf = render_pdf_bytes(
        f"Signal-OS investigation {record.get('inv_id')}", markdown
    )
    get_metrics().incr("api.reports.rendered", tags={"format": fmt})
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="signal-os-{record.get("inv_id")}.pdf"',
        },
    )


@router.get("/investigate/{inv_id}/export")
async def export_report(inv_id: str, request: Request,
                        format: Optional[str] = Query(default=None),
                        bundle: bool = Query(default=False)):
    """Download one investigation's report as an artefact.

    With ``bundle=true`` the response is a ZIP containing the report in all
    available formats plus the structured JSON, so an analyst keeps everything
    from a single request. Otherwise the response is the artefact in the
    requested format. Both are assembled in memory.

    Args:
        inv_id: The investigation identifier.
        request: Used to stamp the audit trail.
        format: ``markdown`` (default), ``pdf`` or ``json``.
        bundle: Return a ZIP of every format instead of a single artefact.

    Returns:
        A file download (``application/pdf``, ``text/markdown`` or
        ``application/json``), or ``application/zip`` when ``bundle=true``.

    Raises:
        HTTPException: 400 (bad format), 404 (unknown investigation), 409 (still
            running), 501 (pdf export disabled).
    """
    fmt = _validate_format(format)
    record, report = await _load(inv_id)
    _guard_finished(record)

    markdown = _markdown_for(record, report)
    stem = f"signal-os-{record.get('inv_id')}"

    if bundle:
        payload = build_zip({
            "report.md": markdown,
            "report.json": json.dumps(_json_for(record, report), indent=2, default=str),
            "agents.json": json.dumps(record.get("agents") or [], indent=2, default=str),
            "steps.json": json.dumps(record.get("steps") or [], indent=2, default=str),
            "report.pdf": render_pdf_bytes(
                f"Signal-OS investigation {record.get('inv_id')}", markdown),
        })
        get_metrics().incr("api.reports.exported", tags={"format": "zip"})
        await _audit(request, record, "zip", len(payload))
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{stem}.zip"',
            },
        )

    if fmt == "json":
        body = json.dumps(_json_for(record, report), indent=2, default=str)
        media_type, extension = "application/json", "json"
    elif fmt == "pdf":
        body = render_pdf_bytes(f"Signal-OS investigation {record.get('inv_id')}", markdown)
        media_type, extension = "application/pdf", "pdf"
    else:
        body = markdown
        media_type, extension = "text/markdown; charset=utf-8", "md"

    get_metrics().incr("api.reports.exported", tags={"format": fmt})
    # Audit the real on-wire size in bytes. `body` is bytes for the binary
    # formats and str for the text ones; `len(str)` counts *characters*, which
    # understated the audit trail by the UTF-8 expansion of every non-ASCII
    # character (em dashes, accented names).
    size = len(body) if isinstance(body, bytes) else len(body.encode("utf-8"))
    await _audit(request, record, fmt, size)
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{stem}.{extension}"',
        },
    )


async def _audit(request: Request, record: dict, fmt: str, size: int) -> None:
    """Record an export in the audit trail, ignoring failures.

    Args:
        request: The request that triggered the export.
        record: The exported investigation.
        fmt: The exported format.
        size: Payload size in bytes.
    """
    try:
        store = await resolve_store()
        await store.audit({
            "event": "report.exported", "inv_id": record.get("inv_id"),
            "case_id": record.get("case_id"), "format": fmt, "bytes": size,
            "principal": current_principal(request).get("id")})
    except Exception:  # noqa: BLE001 — auditing is best-effort
        pass

