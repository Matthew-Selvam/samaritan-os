"""investigate.py — investigation submission, search and the model arena.

Owned by WS-API. Ported from the handlers that lived in ``main.py``; every path,
request body and response body is preserved because CONTRACTS.md §10 pins them
as backwards-compatibility requirements:

    POST /api/investigate       -> {inv_id, case_id, status}  (returns at once)
    GET  /api/investigate/{id}  -> the stored investigation record
    POST /api/investigate-sync  -> the finished record in one round trip
    GET  /api/investigations    -> newest-first list
    POST /api/search            -> SCOUT directly
    POST /api/photo-search      -> uploaded media -> IRIS pipeline
    POST /api/name-search       -> person-name sweep

plus the endpoint ``main.py``'s docstring advertised but never implemented:

    POST /api/compare           -> the "model arena": one input, N model configs

Storage moved from ``main.py``'s module-level ``INVESTIGATIONS`` dict to
``store`` (CONTRACTS.md §4) and the WS fan-out to the hub in ``api/ops.py``, but
submit still returns immediately and the pipeline still runs asynchronously —
the fire-and-forget contract is the entire point of that endpoint.

Two defects in the original are fixed rather than copied:

  * photo uploads used ``tempfile.NamedTemporaryFile(delete=False)`` and never
    removed the file, leaking one artefact per upload for the life of the host.
    Uploads now land in a per-investigation temp directory deleted in the
    pipeline's ``finally``, and the request is rejected *before* any write when
    the payload exceeds ``MAX_UPLOAD_BYTES`` or the content type is not an
    image.
  * inputs were accepted at any length. Every request model here caps free text.

Deliberate, documented deviation: ``GET /api/investigate/{inv_id}`` answers an
unknown id with HTTP 404 and the body ``{"error": "not found"}`` instead of
main.py's 200-with-an-error-body. The body key is unchanged and the frontend
client documents "throws ApiError when the id is unknown", so a real status
code is the contract both sides actually want.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, ConfigDict, Field

from api.ops import (
    ALLOWED_IMAGE_TYPES,
    auth_gate,
    broadcast,
    clamp_limit,
    clamp_offset,
    current_principal,
    max_upload_bytes,
    paged,
    rate_limit_dep,
    resolve_store,
    sanitize_filename,
    _maybe_import,
)
from observability import get_logger, get_metrics

log = get_logger("signal-os.api.investigate")

router = APIRouter(prefix="/api", tags=["investigate"])

#: Hard ceiling on one investigation input, in characters.
MAX_INPUT_CHARS = 8192


# ════════════════════════════════════════════════════════════════════════════
# Request models
# ════════════════════════════════════════════════════════════════════════════
#
# `schemas.py` (WS-SEC) is the canonical home for these per CONTRACTS.md §8 and
# is the preferred definition once it exists; it may legitimately be absent
# while this package builds. Each model is therefore declared locally with
# identical fields and long-form constraints, then swapped for the WS-SEC class
# at the bottom of this section if one is importable. Exactly one definition is
# ever live per process, so the two cannot drift.


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


class _InvestigateRequest(BaseModel):
    """Universal input: text, username, email, domain, URL or a file path.

    Attributes:
        input: The target to investigate (length-capped).
        input_type: Force a routing decision; auto-detected when omitted.
        case_id: Attach to an existing case, or let the server create one.
        deep: Request the deeper connector sweep.
        lang: Response language hint for LLM-backed agents.
    """

    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    input_type: Optional[str] = Field(default=None, max_length=64)
    case_id: Optional[str] = Field(default=None, max_length=64)
    deep: bool = False
    lang: Optional[str] = Field(default=None, max_length=16)


class _SearchRequest(BaseModel):
    """Free-form OSINT search request.

    Attributes:
        query: What to search for.
        engines: Search backends to federate across.
        use_dorks: Generate advanced dork queries alongside plain search.
        case_id: Optional case to bind the run to.
        deep: Request the deeper connector sweep.
        lang: Response language hint.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2048)
    engines: list[str] = Field(default_factory=lambda: ["google", "bing", "yandex"],
                                max_length=8)
    use_dorks: bool = True
    case_id: Optional[str] = Field(default=None, max_length=64)
    deep: bool = False
    lang: Optional[str] = Field(default=None, max_length=16)


class _NameSearchRequest(BaseModel):
    """Person-name OSINT sweep request.

    Attributes:
        name: The person's name.
        location: Optional locality hint that narrows the search.
        sources: Optional allowlist of OSINT surfaces (e.g. social, breach).
        case_id: Optional case to bind the run to.
        deep: Request the deeper connector sweep.
        lang: Response language hint.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    location: Optional[str] = Field(default=None, max_length=256)
    sources: list[str] = Field(default_factory=list, max_length=16)
    case_id: Optional[str] = Field(default=None, max_length=64)
    deep: bool = False
    lang: Optional[str] = Field(default=None, max_length=16)


class _CompareRequest(BaseModel):
    """Model-arena request: run one input through several model configs.

    Attributes:
        input: The subject under analysis.
        task: What to ask the models. ``analysis`` returns prose, ``entity``
            asks each model for structured entities/signals as JSON.
        models: Model ids to race. Defaults to whatever ``llm.available_models()``
            reports, capped at ``MAX_ARENA_MODELS``.
        system: Optional system prompt shared by every contestant.
        temperature: Sampling temperature handed to every model.
        max_tokens: Per-model completion budget.
    """

    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    task: str = Field(default="analysis", max_length=32, pattern="^(analysis|entity)$")
    models: list[str] = Field(default_factory=list, max_length=8)
    system: Optional[str] = Field(default=None, max_length=2048)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=64, le=8192)


#: Effective request models (WS-SEC definitions when present).
InvestigateRequest = _pick_schema(_InvestigateRequest, "InvestigateRequest")
SearchRequest = _pick_schema(_SearchRequest, "SearchRequest")
NameSearchRequest = _pick_schema(_NameSearchRequest, "NameSearchRequest")
CompareRequest = _pick_schema(_CompareRequest, "CompareRequest")


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════


def _now_iso() -> str:
    """Return the current UTC instant as ISO-8601 with a ``Z`` suffix.

    Returns:
        An ISO-8601 timestamp.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    """Mint a short, collision-resistant identifier.

    Matches main.py's ``str(uuid.uuid4())[:8]`` length so ids stay URL-friendly
    and remain recognisable to anything keyed on the old format.

    Returns:
        An 8-character hex string.
    """
    return str(uuid.uuid4())[:8]


def _build_report(result: Any, agent_results: list[dict]) -> dict:
    """Shape an APEX ``AgentResult`` into the report the frontend expects.

    Preserved verbatim from ``main.py`` — the frontend ``Report`` type and the
    persisted case stats both depend on these exact keys.

    Args:
        result: The APEX ``AgentResult``.
        agent_results: The serialised per-agent results.

    Returns:
        The report dict.
    """
    out = getattr(result, "output", None) or {}
    confidence = getattr(result, "confidence", 0.0) or 0.0
    return {
        "summary": (
            f"Investigation complete — {out.get('input_type', 'unknown')}; "
            f"{len(agent_results)} agents, confidence {confidence:.0%}"
        ),
        "input_type": out.get("input_type"),
        "agents_activated": out.get("agents_activated", []),
        "entities": out.get("entities", []),
        "signals": out.get("signals", []),
        "graph": out.get("graph", {"nodes": [], "edges": []}),
        "timeline": out.get("timeline", []),
        "markdown": out.get("report_markdown"),
        "confidence": confidence,
        "latency_s": getattr(result, "latency_s", 0.0),
    }


def _get(req: Any, field: str, default: Any = None) -> Any:
    """Read a field from a request model that may not define it.

    The effective request models come from ``schemas.py`` (WS-SEC) once it
    lands, and their field sets are deliberately tighter than this router's
    local equivalents: ``NameSearchRequest`` has no ``deep``/``lang``,
    ``InvestigateRequest`` has no ``lang``, and ``CompareRequest`` calls its
    subject ``prompt`` rather than ``input``. Direct attribute access would
    raise ``AttributeError`` on whichever variant is live, so every optional
    read goes through here.

    Args:
        req: The request model (or any object).
        field: Attribute name to read.
        default: Value returned when the attribute is absent or ``None``.

    Returns:
        The field value, or ``default``.
    """
    value = getattr(req, field, None)
    return default if value is None else value


def _subject(req: Any) -> str:
    """Return an arena request's subject text.

    ``schemas.CompareRequest`` names it ``prompt``; the local fallback used
    ``input``. Both spellings are accepted.

    Args:
        req: The comparison request.

    Returns:
        The subject text to send to every model.
    """
    for field in ("prompt", "input", "text", "query"):
        value = getattr(req, field, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _context_for(req: Any, *, inv_id: Optional[str] = None,
                 emit: Optional[Any] = None) -> dict:
    """Build the APEX ``context`` dict for one run.

    Only the keys CONTRACTS.md §2 documents as agent-visible are passed, so no
    agent has to guess at private request state.

    Args:
        req: The validated request model.
        inv_id: Investigation id, added for traceability.
        emit: Optional async callback APEX relays step lines to.

    Returns:
        The context dict.
    """
    context: dict[str, Any] = {}
    if emit is not None:
        context["emit"] = emit
    if _get(req, "input_type"):
        context["input_type"] = req.input_type
    if _get(req, "case_id"):
        context["case_id"] = req.case_id
    if _get(req, "deep", False):
        context["deep"] = True
    if _get(req, "lang"):
        context["lang"] = _get(req, "lang")
    if inv_id:
        context["inv_id"] = inv_id
    return context


async def _audit(event: dict) -> None:
    """Append an audit event, ignoring every failure.

    Args:
        event: Event payload; redacted by the store before storage.
    """
    try:
        store = await resolve_store()
        await store.audit(event)
    except Exception as exc:  # noqa: BLE001 — auditing is best-effort
        log.debug("audit write failed: %s", type(exc).__name__)


async def _run_pipeline(inv_id: str, input_value: str, *,
                        input_type: Optional[str] = None,
                        case_id: Optional[str] = None,
                        deep: bool = False,
                        lang: Optional[str] = None,
                        workdir: Optional[str] = None) -> None:
    """Run the investigation pipeline, delegating to the shared runtime.

    This is a thin wrapper around :func:`runtime.run_pipeline`, which owns the
    store updates, the step broadcast and the report shaping. Delegating rather
    than reimplementing keeps this router and ``main.py``'s legacy endpoints
    consistent: both write the same record shape and publish to the same
    WebSocket bus, so a run started through either path renders identically.

    Taking scalars instead of a request model is deliberate — the request models
    come from ``schemas.py`` and their field sets differ between the canonical
    and fallback definitions, so a pipeline driver that took the model would
    break whenever a field was added or renamed upstream.

    The wrapper adds exactly the two things ``runtime.run_pipeline`` knows
    nothing about — the upload temp directory, which is removed in a ``finally``
    no matter how the run ends, and the audit event.

    Args:
        inv_id: Investigation to run and update.
        input_value: The pipeline subject.
        input_type: Optional forced routing type; auto-detected when omitted.
        case_id: The owning case.
        deep: Request the deeper agent pass.
        lang: Optional response-language hint.
        workdir: Optional directory holding uploaded media; always removed, even
            when the pipeline raises.
    """
    started = time.monotonic()
    try:
        from runtime import run_pipeline

        record = await run_pipeline(
            inv_id,
            input_value,
            input_type=input_type,
            case_id=case_id,
            deep=deep,
            lang=lang,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface orchestration failure to the client
        log.exception("pipeline failed for %s", inv_id)
        message = str(exc)
        try:
            store = await resolve_store()
            await store.update_investigation(inv_id, {
                "status": "error", "error": message, "ended_at": _now_iso()})
        except Exception:  # noqa: BLE001 — the run already failed; do not mask it
            pass
        await broadcast(inv_id, {"type": "error", "error": message, "message": message})
        get_metrics().incr("api.investigate.error")
        return
    finally:
        # The uploaded media is only needed while the pipeline is reading it.
        if workdir:
            _cleanup_workdir(workdir)

    record = record if isinstance(record, dict) else {}
    status = str(record.get("status") or "unknown")
    if status == "done":
        await _persist_report(inv_id, record)
        get_metrics().incr("api.investigate.done")
    elif status == "error":
        get_metrics().incr("api.investigate.error")
    get_metrics().timing("api.investigate.duration", time.monotonic() - started)
    await _audit({"event": "investigate.completed", "inv_id": inv_id,
                  "case_id": case_id, "status": status,
                  "agents": len(record.get("agents") or [])})


async def _persist_report(inv_id: str, record: dict) -> None:
    """Save a finished run's rendered markdown so `/report` and `/export` work.

    Args:
        inv_id: The investigation that finished.
        record: The record ``run_pipeline`` returned.
    """
    report = record.get("report")
    if not isinstance(report, dict):
        return
    try:
        store = await resolve_store()
        await store.save_report(inv_id, report.get("markdown") or "", {
            "case_id": str(record.get("case_id") or ""),
            "format": "markdown",
            "input_type": record.get("input_type"),
        })
    except Exception:  # noqa: BLE001 — report storage is a convenience
        pass


def _cleanup_workdir(path: str) -> None:
    """Remove an upload directory, never raising.

    Args:
        path: Directory to delete.
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not remove upload dir: %s", type(exc).__name__)


async def _spawn(inv_id: str, input_value: str, *, input_type: Optional[str] = None,
                 case_id: Optional[str] = None, deep: bool = False,
                 lang: Optional[str] = None,
                 workdir: Optional[str] = None) -> None:
    """Fire-and-forget wrapper around :func:`_run_pipeline`.

    Mirrors main.py's ``asyncio.ensure_future``: the request returns immediately
    and the caller polls ``GET /api/investigate/{inv_id}`` or subscribes to the
    WebSocket. The task is tracked in :data:`_BACKGROUND` so it is not
    garbage-collected mid-flight.

    Args:
        inv_id: Investigation being run.
        input_value: The pipeline subject.
        input_type: Optional forced routing type.
        case_id: The owning case.
        deep: Request the deeper agent pass.
        lang: Optional response-language hint.
        workdir: Optional upload directory to clean up when the run ends.
    """
    task = asyncio.ensure_future(_run_pipeline(
        inv_id, input_value, input_type=input_type, case_id=case_id,
        deep=deep, lang=lang, workdir=workdir,
    ))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


#: Strong references to in-flight pipeline tasks (CPython only keeps weak ones).
_BACKGROUND: set[asyncio.Task] = set()


# ════════════════════════════════════════════════════════════════════════════
# Upload handling
# ════════════════════════════════════════════════════════════════════════════


async def _store_upload(file: UploadFile, case_id: str) -> tuple[str, str]:
    """Validate and persist an uploaded image into a per-case temp directory.

    Enforces the size cap by *counting* bytes as they stream in, so an oversized
    body is rejected and truncated rather than buffered whole; enforces an image
    content-type allowlist; and sanitises the filename before it can influence a
    path.

    Args:
        file: The inbound upload.
        case_id: Case the upload belongs to; names the temp directory.

    Returns:
        ``(path, safe_name)`` for the stored file.

    Raises:
        HTTPException: 415 for a non-image content type, 413 when the upload
            exceeds ``MAX_UPLOAD_BYTES``.
    """
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type and content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail={
            "error": "unsupported media type",
            "content_type": content_type,
            "allowed": sorted(ALLOWED_IMAGE_TYPES),
        })

    limit = max_upload_bytes()
    safe_name = sanitize_filename(file.filename or "upload", fallback="upload")
    extension = os.path.splitext(safe_name)[1].lower()[:12]
    if extension and not extension[1:].isalnum():
        extension = ""
    workdir = tempfile.mkdtemp(prefix=f"signal-{case_id}-")
    target = os.path.join(workdir, f"{safe_name[:64] or 'upload'}{extension}")

    written = 0
    try:
        with open(target, "wb") as handle:
            while True:
                chunk = await file.read(1024 * 256)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise HTTPException(status_code=413, detail={
                        "error": "upload too large",
                        "limit_bytes": limit,
                    })
                handle.write(chunk)
    except HTTPException:
        _cleanup_workdir(workdir)
        raise
    except Exception as exc:  # noqa: BLE001
        _cleanup_workdir(workdir)
        raise HTTPException(status_code=500,
                            detail={"error": f"upload failed: {type(exc).__name__}"}) from exc
    finally:
        try:
            await file.close()
        except Exception:  # noqa: BLE001
            pass

    if written == 0:
        _cleanup_workdir(workdir)
        raise HTTPException(status_code=400, detail={"error": "empty upload"})

    # A client may lie about content-type; sniff the magic bytes as a second gate.
    if not _looks_like_image(target):
        _cleanup_workdir(workdir)
        raise HTTPException(status_code=415,
                            detail={"error": "file content is not a recognised image"})

    get_metrics().incr("api.photo_search.uploaded", tags={"bytes_class": _size_class(written)})
    return target, safe_name


def _size_class(size: int) -> str:
    """Bucket a byte count for low-cardinality metrics tagging.

    Args:
        size: Number of bytes.

    Returns:
        One of ``"<1KB"``, ``"<1MB"``, ``"<10MB"`` or ``">=10MB"``.
    """
    if size < 1024:
        return "<1KB"
    if size < 1024 * 1024:
        return "<1MB"
    if size < 10 * 1024 * 1024:
        return "<10MB"
    return ">=10MB"


def _looks_like_image(path: str) -> bool:
    """Check a file's magic bytes against known image signatures.

    Args:
        path: File to inspect.

    Returns:
        ``True`` when the header matches a supported image format. An empty
        snippet (unreadable file) is treated as *not* an image.
    """
    signatures: tuple[bytes, ...] = (
        b"\xff\xd8\xff",                        # jpeg
        b"\x89PNG\r\n\x1a\n",                  # png
        b"GIF87a", b"GIF89a",                  # gif
        b"BM",                                 # bmp
        b"II*\x00", b"MM\x00*",                # tiff
    )
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    if any(head.startswith(sig) for sig in signatures):
        return True
    # RIFF/WEBP and ISO-BMFF/HEIC both start with a box header.
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return head[4:8] == b"ftyp"


# ════════════════════════════════════════════════════════════════════════════
# Routes — ported from main.py
# ════════════════════════════════════════════════════════════════════════════


@router.post("/investigate")
async def submit_investigation(req: InvestigateRequest, request: Request):
    """Queue an investigation and return immediately.

    Identical contract to main.py's handler — the response is
    ``{inv_id, case_id, status}`` with ``status="queued"``, and APEX runs in the
    background so a multi-minute pipeline never blocks the client. Progress is
    available via ``GET /api/investigate/{inv_id}`` or the pipeline WebSocket.

    Args:
        req: The investigation request.
        request: Used to stamp the audit trail with the caller.

    Returns:
        ``{"inv_id", "case_id", "status"}``.

    Raises:
        HTTPException: 401 when auth rejects the caller (WS-SEC).
    """
    inv_id = new_id()
    case_id = _get(req, "case_id") or new_id()
    deep = bool(_get(req, "deep", False))
    payload = {
        "inv_id": inv_id,
        "case_id": case_id,
        "input": req.input,
        "input_type": _get(req, "input_type"),
        "status": "queued",
        "steps": [],
        "report": None,
        "started_at": _now_iso(),
        "deep": deep,
        "lang": _get(req, "lang"),
    }
    store = await resolve_store()
    await store.save_investigation(payload)
    await _spawn(
        inv_id, req.input,
        input_type=_get(req, "input_type"),
        case_id=case_id, deep=deep,
        lang=_get(req, "lang"),
    )
    get_metrics().incr("api.investigate.submitted")
    await _audit({"event": "investigate.submitted", "inv_id": inv_id, "case_id": case_id,
                  "principal": current_principal(request).get("id")})
    return {"inv_id": inv_id, "case_id": case_id, "status": "queued"}


@router.get("/investigate/{inv_id}")
async def get_investigation(inv_id: str):
    """Fetch one investigation by id.

    Returns the stored record — ``{inv_id, case_id, input, input_type, status,
    steps, agents, report, ...}`` — unchanged from main.py, plus the extra
    fields APEX produces.

    Args:
        inv_id: Investigation identifier.

    Returns:
        The stored investigation record.

    Raises:
        HTTPException: 404 when no such investigation exists.
    """
    inv_id = str(inv_id or "")[:64]
    store = await resolve_store()
    record = await store.get_investigation(inv_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": "not found"})
    return record


@router.post("/investigate-sync")
async def submit_investigation_sync(req: InvestigateRequest):
    """Run a full investigation to completion inside one request/response.

    Serverless platforms (Vercel Functions) isolate each invocation: there is no
    guarantee a background task survives past the response, and no guarantee a
    later ``GET`` lands on the same instance as the record the ``POST`` wrote.
    This endpoint sidesteps both problems by never returning until APEX has
    finished, so the full report comes back in one round trip. There is no
    WebSocket step-streaming here (not applicable outside a persistent process) —
    the frontend simply awaits the response.

    The record is also persisted, so ``/report`` and ``/export`` work for
    synchronous runs.

    Args:
        req: The investigation request.

    Returns:
        ``{inv_id, case_id, status, input, input_type, agents, report}``, or
        ``status="error"`` with the failure message if orchestration failed.
    """
    inv_id = new_id()
    case_id = _get(req, "case_id") or new_id()
    deep = bool(_get(req, "deep", False))

    # The store handle is a coroutine: bind it first, then use it. Writing
    # `await resolve_store().save_investigation(...)` here returned the
    # *coroutine* rather than a store, so every attribute access on it raised
    # AttributeError and this endpoint 500'd.
    store = await resolve_store()
    await store.save_investigation({
        "inv_id": inv_id,
        "case_id": case_id,
        "input": req.input,
        "input_type": req.input_type,
        "status": "queued",
        "steps": [],
        "report": None,
        "started_at": _now_iso(),
        "deep": deep,
    })

    started = time.monotonic()
    try:
        from runtime import run_pipeline

        record = await run_pipeline(
            inv_id,
            req.input,
            input_type=_get(req, "input_type"),
            case_id=case_id,
            deep=deep,
            lang=_get(req, "lang"),
        )
    except Exception as exc:  # noqa: BLE001 — surface orchestration failure to the client
        log.exception("sync pipeline failed for %s", inv_id)
        message = str(exc)
        try:
            await store.update_investigation(inv_id, {
                "status": "error", "error": message, "ended_at": _now_iso()})
        except Exception:  # noqa: BLE001 — do not mask the original failure
            pass
        get_metrics().incr("api.investigate.sync_error")
        return {"inv_id": inv_id, "case_id": case_id, "status": "error", "error": message}

    record = record if isinstance(record, dict) else {}
    status = str(record.get("status") or "done")
    report = record.get("report") if isinstance(record.get("report"), dict) else {}
    if status == "done":
        await _persist_report(inv_id, record)
        get_metrics().incr("api.investigate.sync_done")
    get_metrics().timing("api.investigate.sync_duration", time.monotonic() - started)
    return {
        "inv_id": inv_id,
        "case_id": case_id,
        "status": status,
        "input": req.input,
        "input_type": record.get("input_type"),
        "agents": record.get("agents") or [],
        "report": report,
    }


@router.get("/investigations")
async def list_investigations(case_id: Optional[str] = None, limit: int = 50,
                              offset: int = 0, response: Response = None):

    """List investigations, newest first.

    Returns a **bare JSON array** because main.py did and
    ``frontend/components/CasePanel.tsx`` indexes the response directly; the
    ``limit``/``offset`` window is applied here and the unpaginated total is
    published as the ``X-Total-Count`` header.

    Args:
        case_id: Restrict to one case.
        limit: Page size (capped at 200).
        offset: Rows to skip.
        response: Used to publish ``X-Total-Count``.

    Returns:
        A list of ``{inv_id, case_id, input, input_type, status, created_at,
        agents, report}`` summaries.
    """
    size = clamp_limit(limit)
    start = clamp_offset(offset)
    store = await resolve_store()
    rows = await store.list_investigations(case_id=case_id, limit=size + start + 1)
    total = len(rows)
    page = rows[start:start + size]
    if response is not None:
        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Limit"] = str(size)
        response.headers["X-Offset"] = str(start)
    return [
        {
            "inv_id": row.get("inv_id"),
            "case_id": row.get("case_id"),
            "status": row.get("status"),
            "input_type": row.get("input_type"),
            "input": str(row.get("input", ""))[:80],
            "created_at": row.get("created_at"),
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
            "agents": row.get("agents") or [],
            "report": row.get("report"),
            "error": row.get("error"),
        }
        for row in page
    ]


@router.post("/search")
async def run_search(req: SearchRequest):
    """Run SCOUT directly — dork generation plus search federation.

    Args:
        req: The search request.

    Returns:
        ``{query, agent, status, output, confidence, reasoning, latency_s,
        steps, error}`` — main.py's shape, with ``output`` carrying the
        frontend ``SearchResponse`` fields (results/entities/signals/markdown)
        when SCOUT produced them.
    """
    from agents.scout import ScoutAgent

    scout = ScoutAgent()
    result = await scout.run(
        req.query,
        context={
            "engines": _get(req, "engines") or ["google", "bing", "yandex"],
            "use_dorks": bool(_get(req, "use_dorks", True)),
            "case_id": _get(req, "case_id"),
            "deep": bool(_get(req, "deep", False)),
            "lang": _get(req, "lang"),
        },
    )
    get_metrics().incr("api.search.run")
    get_metrics().timing("api.search.duration", getattr(result, "latency_s", 0.0) or 0.0)
    return {
        "query": req.query,
        "agent": result.agent,
        "status": result.status,
        "output": result.output,
        "confidence": result.confidence,
        "reasoning": result.reasoning,
        "latency_s": result.latency_s,
        "steps": list(getattr(scout, "_steps", []) or []),
        "entities_found": result.entities_found or [],
        "signals": result.signals or [],
        "error": result.error,
    }


@router.post("/photo-search")
async def photo_search(request: Request,
                       file: UploadFile = File(...),
                       case_id: Optional[str] = Form(default=None),
                       deep: bool = Form(default=False)):
    """Upload a photo and queue the IRIS deanonymisation pipeline.

    The upload is validated (size cap, image content-type allowlist, magic-byte
    sniff, sanitised filename) and stored in a per-case temp directory that is
    deleted as soon as the pipeline finishes — the leak in main.py is fixed.

    Args:
        request: Used to stamp the audit trail.
        file: The uploaded image.
        case_id: Optional existing case to bind the run to.
        deep: Request the deeper sweep.

    Returns:
        ``{inv_id, case_id, status, filename, input_type}``.

    Raises:
        HTTPException: 400 (empty), 413 (too large), 415 (not an image).
    """
    case_id = str(case_id)[:64] if case_id else new_id()
    inv_id = new_id()
    try:
        path, safe_name = await _store_upload(file, case_id)
    except HTTPException as exc:
        get_metrics().incr("api.photo_search.rejected")
        await _audit({"event": "photo_search.rejected", "case_id": case_id,
                      "reason": str(exc.detail)})
        raise

    store = await resolve_store()
    await store.save_investigation({
        "inv_id": inv_id,
        "case_id": case_id,
        "input": f"photo:{safe_name}",
        "input_type": "photo",
        "status": "queued",
        "steps": [],
        "report": None,
        "started_at": _now_iso(),
        "deep": bool(deep),
        "media_path": path,
    })
    await _spawn(inv_id, path, input_type="photo", case_id=case_id,
                 deep=deep, workdir=os.path.dirname(path))
    get_metrics().incr("api.photo_search.submitted")
    await _audit({"event": "photo_search.submitted", "inv_id": inv_id,
                  "case_id": case_id, "filename": safe_name})
    return {
        "inv_id": inv_id,
        "case_id": case_id,
        "status": "queued",
        "filename": safe_name,
        "input_type": "photo",
    }


@router.post("/name-search")
async def name_search(req: NameSearchRequest):
    """Queue a person-name sweep (PRISM + SCOUT + INK pipeline).

    Args:
        req: The name-search request.

    Returns:
        ``{inv_id, case_id, status, name}`` — main.py's shape.
    """
    inv_id = new_id()
    case_id = _get(req, "case_id") or new_id()
    # ``NameSearchRequest`` (schemas.py) defines name/location/case_id only, so
    # deep/lang are read defensively — the local fallback model has them.
    deep = bool(_get(req, "deep", False))
    lang = _get(req, "lang")
    store = await resolve_store()
    await store.save_investigation({
        "inv_id": inv_id,
        "case_id": case_id,
        "input": req.name,
        "input_type": "person_name",
        "status": "queued",
        "steps": [],
        "report": None,
        "started_at": _now_iso(),
        "location": _get(req, "location"),
        "deep": deep,
    })
    await _spawn(inv_id, req.name, input_type="person_name", case_id=case_id,
                 deep=deep, lang=lang)
    get_metrics().incr("api.name_search.submitted")
    return {"inv_id": inv_id, "case_id": case_id, "status": "queued", "name": req.name}


# ════════════════════════════════════════════════════════════════════════════
# Model arena
# ════════════════════════════════════════════════════════════════════════════

#: Never race more than this many models in one call (wall-clock + cost guard).
MAX_ARENA_MODELS = 8

_ARENA_PROMPTS = {
    "analysis": (
        "You are an OSINT analyst. Analyse the following subject and report what "
        "is publicly knowable about it, what is inferred versus confirmed, and "
        "which open-source collection would resolve the remaining uncertainty. "
        "Be specific and concise.\n\nSubject: {subject}"
    ),
    "entity": (
        "Extract every distinct entity and signal from the following subject. "
        'Respond as JSON: {{"entities": [{{"label","type","value","confidence",'
        '"source"}}], "signals": [{{"type","value","confidence"}}]}}. Use only '
        "these entity types: person, email, username, domain, url, ip, phone, "
        "account, image, face, location, wallet, org, crypto, document, note, "
        "other.\n\nSubject: {subject}"
    ),
}


@router.post("/compare")
async def compare_models(req: CompareRequest, request: Request):
    """Run one input through several model configurations and rank the results.

    The "model arena" main.py's docstring advertised but never implemented. The
    same subject is sent to every contestant through ``llm.get_llm()`` — the only
    sanctioned path to a provider (CONTRACTS.md §1) — concurrently, and each
    response is captured with its latency and token accounting.

    The verdict is computed deterministically from the measured numbers rather
    than by asking a model to judge itself: the winner is the fastest contestant
    that actually produced output, with total tokens and error state as
    tie-breakers. With no provider configured every contestant returns the
    deterministic offline fallback, which is reported honestly as
    ``available_models: []`` plus per-model ``error`` values instead of being
    dressed up as a real comparison.

    Args:
        req: The comparison request.
        request: Used to stamp the audit trail.

    Returns:
        ``{task, input, models, verdict, latency_s}`` where each model entry is
        ``{model, provider, output, latency_s, prompt_tokens, completion_tokens,
        total_tokens, error, ok}``.
    """
    from llm import get_llm

    llm = get_llm()
    requested = [str(m).strip() for m in (_get(req, "models") or []) if str(m).strip()]
    if requested:
        models = requested[:MAX_ARENA_MODELS]
    else:
        models = (llm.available_models() or [])[:MAX_ARENA_MODELS]
    if not models:
        models = ["(default)"]

    # `schemas.CompareRequest` exposes prompt/models/temperature/system/case_id/
    # rounds — no `task`, `input` or `max_tokens`. All optional knobs are read
    # through `_get` so the handler works against either the canonical model or
    # the local fallback, and unknown fields degrade to sane defaults.
    task = str(_get(req, "task", "analysis") or "analysis").lower()
    if task not in _ARENA_PROMPTS:
        task = "analysis"
    subject = _subject(req)
    system = _get(req, "system")
    temperature = float(_get(req, "temperature", 0.2))
    max_tokens = int(_get(req, "max_tokens", 1024) or 1024)

    prompt = _ARENA_PROMPTS[task].format(subject=subject)
    started = time.monotonic()

    async def contestant(model: str) -> dict:
        """Run one model against the arena prompt.

        Args:
            model: Model id, or ``"(default)"`` for provider-default routing.

        Returns:
            One arena entry with output, latency, tokens and error state.
        """
        model_arg = None if model == "(default)" else model
        t0 = time.monotonic()
        try:
            if task == "entity":
                parsed = await llm.complete_json(
                    prompt, system=system, model=model_arg,
                    temperature=temperature, max_tokens=max_tokens,
                    schema_hint='{"entities": [], "signals": []}',
                )
                result = None
                output: Any = parsed
                error = None
                if isinstance(parsed, dict) and parsed.get("error"):
                    error = str(parsed["error"])
            else:
                result = await llm.complete(
                    prompt, system=system, model=model_arg,
                    temperature=temperature, max_tokens=max_tokens,
                )
                output = result.text
                error = result.error
            return {
                "model": model,
                "provider": getattr(result, "provider", None) if result else None,
                "output": output,
                "latency_s": round(time.monotonic() - t0, 3),
                "prompt_tokens": int(getattr(result, "prompt_tokens", 0) or 0) if result else 0,
                "completion_tokens": int(getattr(result, "completion_tokens", 0) or 0) if result else 0,
                "total_tokens": (
                    int(getattr(result, "prompt_tokens", 0) or 0)
                    + int(getattr(result, "completion_tokens", 0) or 0)
                ) if result else 0,
                "error": error,
                "ok": error is None,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad model must not sink the arena
            log.warning("arena contestant %s failed: %s", model, type(exc).__name__)
            return {
                "model": model,
                "provider": None,
                "output": None,
                "latency_s": round(time.monotonic() - t0, 3),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "error": f"{type(exc).__name__}: {exc}",
                "ok": False,
            }

    entries = await asyncio.gather(*(contestant(m) for m in models))
    ranked = sorted(
        (e for e in entries if e["ok"]),
        key=lambda e: (e["latency_s"], e["total_tokens"]),
    )
    winner = ranked[0] if ranked else None
    verdict = {
        "winner": winner["model"] if winner else None,
        "winner_reason": (
            "fastest model that returned output"
            if winner else
            "no model returned output (no provider configured or every call failed)"
        ),
        "ranked": [
            {"model": e["model"], "latency_s": e["latency_s"],
             "total_tokens": e["total_tokens"]}
            for e in ranked
        ],
        "ok_count": sum(1 for e in entries if e["ok"]),
        "error_count": sum(1 for e in entries if not e["ok"]),
        "fastest_model": min(entries, key=lambda e: e["latency_s"])["model"],
        "fewest_tokens_model": min(entries, key=lambda e: e["total_tokens"])["model"],
        "note": (
            "Verdict is computed from measured latency/tokens, not an LLM judge."
        ),
    }
    get_metrics().incr("api.compare.runs", value=len(entries))
    get_metrics().timing("api.compare.duration", time.monotonic() - started)
    await _audit({"event": "compare.run", "models": models, "task": task,
                  "principal": current_principal(request).get("id")})
    return {
        "task": task,
        "input": subject,
        "prompt": subject,
        "case_id": _get(req, "case_id"),
        "rounds": int(_get(req, "rounds", 1) or 1),
        "available_models": llm.available_models(),
        "models": list(entries),
        "verdict": verdict,
        "latency_s": round(time.monotonic() - started, 3),
    }


# Auth + rate limiting cover every route above, matching how WS-MAIN mounts this
# router: dependencies are attached at the router so a new route cannot ship
# unguarded.
router.dependencies.extend([Depends(auth_gate), Depends(rate_limit_dep)])