"""test_api.py — integration tests for the WS-API router layer.

Builds a *local* FastAPI app that mounts only this package's routers
(``main.py`` belongs to WS-MAIN and is deliberately not touched), then exercises
every route group end to end:

  * health / deep health / metrics / config
  * investigate submit -> poll -> investigations list
  * investigate-sync, search, name-search, photo-search, compare
  * cases CRUD round trip, stats, entities, timeline, zip export
  * agents registry, routing DAG, single-agent descriptor
  * reports in all three formats, export artefact and zip bundle
  * rate-limit headers and the WebSocket pipeline

The store is replaced with an in-memory instance via ``store.set_store`` so the
suite is hermetic: no Postgres, no Redis, no network, no ``/tmp`` residue.

Run with::

    cd backend && .venv/bin/python -m pytest tests/test_api.py -q
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

# `backend/` is the import root for `main:app` (see main.py's uvicorn call), so
# make sure the suite works no matter where pytest was invoked from.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# WS-SEC's default `investigate` budget is 6 rpm / burst 2, which several tests
# here legitimately exceed (each `done_inv` fixture alone submits a run, and the
# photo-search test submits another). Raise it so only the dedicated rate-limit
# test — which pins its own budget — ever sees a 429.
#
# These are set in this module rather than in conftest.py on purpose: they must
# not leak into the rest of the suite. Setting them process-wide broke
# tests/test_security.py's rate-limit contract tests, which assert that the
# `investigate` scope is *stricter* than `read` — a global override makes it
# looser and fails them. `restore_limits` puts the real values back.
_LIMITS_RAISED = {
    "RATE_LIMIT_SCOPE_RPM.investigate": "600",
    "RATE_LIMIT_BURST.investigate": "600",
    "RATE_LIMIT_SCOPE_RPM.search": "600",
    "RATE_LIMIT_BURST.search": "600",
}


@pytest.fixture(autouse=True, scope="module")
def _raised_api_limits():
    """Raise this module's limiter budgets, then restore them exactly.

    Scope is ``module``, so the override is confined to ``test_api.py`` and
    torn down before any other test module can observe it.
    """
    previous = {key: os.environ.get(key) for key in _LIMITS_RAISED}
    os.environ.update(_LIMITS_RAISED)
    try:
        # Buckets capture their limits at construction, so drop the ones built
        # under the previous env before yielding.
        _clear_api_buckets()
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _clear_api_buckets()


def _clear_api_buckets() -> None:
    """Drop every rate-limit bucket so limits are re-read from the env."""
    try:
        import rate_limit as rate_limit_module
    except Exception:  # noqa: BLE001 — optional module
        return
    tables = getattr(rate_limit_module, "_BUCKETS", None)
    if isinstance(tables, dict):
        for table in tables.values():
            if isinstance(table, dict):
                table.clear()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import store as store_mod  # noqa: E402
from api.agents import router as agents_router  # noqa: E402
from api.cases import CaseCreate  # noqa: E402
from api.cases import router as cases_router  # noqa: E402
from api.investigate import router as investigate_router  # noqa: E402
from api.ops import router as ops_router, ws_router  # noqa: E402
from api.reports import router as reports_router  # noqa: E402

# A 1x1 PNG — the smallest real image, so the magic-byte gate accepts it.
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def build_app() -> FastAPI:
    """Build the local test app with every WS-API router mounted.

    This mirrors what ``main.py`` does minus the app-owned wiring (CORS,
    startup probes, middleware), so the tests exercise exactly the routers this
    workstream owns.

    Returns:
        A configured ``FastAPI`` instance.
    """
    app = FastAPI(title="Signal-OS (api routers test app)", version="0.1.0")
    app.include_router(ops_router)
    app.include_router(investigate_router)
    app.include_router(cases_router)
    app.include_router(agents_router)
    app.include_router(reports_router)
    app.include_router(ws_router)
    return app


@pytest.fixture()
def client() -> TestClient:
    """Yield a ``TestClient`` backed by an isolated in-memory store.

    Returns:
        A client with ``backend/`` as its working directory, so any stray file
        a route writes is visible to the temp-dir assertions rather than
        polluting the repo.
    """
    from api.ops import install_cors

    app = build_app()
    install_cors(app)

    original_cwd = Path.cwd()
    previous_store = store_mod._store
    os.chdir(BACKEND_ROOT)

    # The routers are genuinely auth-guarded now (auth_gate is in the router
    # constructor), so the suite must run with auth off or every call 401s.
    # Patched on the module rather than via env so it holds regardless of the
    # ambient ENVIRONMENT.
    import auth as auth_mod

    previous_auth_enabled = auth_mod.auth_enabled
    auth_mod.auth_enabled = lambda: False

    # The routers resolve storage through `runtime.get_store_handle`, which
    # memoises the handle on first use. Installing the memory store has to go
    # through `runtime` too — calling `store.set_store` alone would leave the
    # routers reading (and writing) a different instance than the test asserts
    # on, which is exactly the multi-instance bug the runtime module exists to
    # prevent.
    import runtime as runtime_mod

    async def _install() -> None:
        memory = store_mod.MemoryStore(max_rows=1000)
        await memory.start()
        await store_mod.set_store(memory)
        runtime_mod._STORE = memory
        runtime_mod._STORE_READY = True

    import asyncio as _asyncio

    try:
        _asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_install())
        with TestClient(app) as test_client:
            test_client.app_instance = app  # type: ignore[attr-defined]
            yield test_client
    finally:
        auth_mod.auth_enabled = previous_auth_enabled
        store_mod._store = previous_store
        runtime_mod._STORE = None
        runtime_mod._STORE_READY = False
        os.chdir(original_cwd)


@pytest.fixture()
def done_inv(client: TestClient) -> dict:
    """Create a case, run one investigation, and wait for it to finish.

    The pipeline runs agents against live connectors, so it is bounded by a
    generous poll timeout and the test only asserts on the *shape* of the result
    — the assertions must hold whether the run produced findings or degraded to
    offline fallbacks.

    Args:
        client: The test client.

    Returns:
        ``{"inv_id", "case_id", "record"}``.
    """
    case = client.post("/api/cases", json={"name": "Test Case", "tags": ["pytest"]}).json()
    case_id = case["case_id"]

    submitted = client.post("/api/investigate", json={
        "input": "pytest-test-target@example.com", "case_id": case_id,
    })
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["status"] == "queued"
    assert body["case_id"] == case_id
    inv_id = body["inv_id"]

    record: dict = {}
    for _ in range(150):
        response = client.get(f"/api/investigate/{inv_id}")
        # Polling a single run legitimately costs many requests, so allow the
        # documented 429 rather than treating it as a failure: the point of this
        # fixture is the pipeline's terminal state, not the limiter.
        if response.status_code == 429:
            __import__("time").sleep(0.5)
            continue
        assert response.status_code == 200, response.text
        record = response.json()
        if record.get("status") in ("done", "error"):
            break
        __import__("time").sleep(0.2)

    assert record.get("status") in ("done", "error"), record
    return {"inv_id": inv_id, "case_id": case_id, "record": record}


# ── ops ──────────────────────────────────────────────────────────────────────


def test_health(client: TestClient) -> None:
    """`GET /api/health` keeps the legacy body and adds dependency state."""
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    # Legacy fields main.py promised — must not regress.
    assert body["status"] in ("ok", "degraded", "down")
    assert body["platform"] == "signal-os"
    assert body["version"] == "0.1.0"
    # Additions the frontend HealthStatus type documents.
    assert "uptime_s" in body
    assert "store" in body and "cache" in body and "llm" in body


def test_health_deep_never_fails(client: TestClient) -> None:
    """`GET /api/health/deep` returns 200 with a verdict for every dependency."""
    response = client.get("/api/health/deep")
    assert response.status_code == 200
    body = response.json()
    assert set(body["checks"]) >= {"postgres", "redis", "neo4j", "qdrant", "minio"}
    for name, check in body["checks"].items():
        assert isinstance(check["ok"], bool), name
        assert "detail" in check, name


def test_metrics(client: TestClient) -> None:
    """`GET /api/metrics` exposes counters plus the API block."""
    response = client.get("/api/metrics")
    assert response.status_code == 200
    body = response.json()
    assert "counters" in body and "gauges" in body
    assert body["api"]["upload_max_bytes"] > 0


def test_config_redacts_secrets(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /api/config` masks secret-shaped values but still reports presence."""
    monkeypatch.setenv("SIGNAL_API_KEY", "super-secret-value")
    response = client.get("/api/config")
    assert response.status_code == 200
    raw = response.text
    assert "super-secret-value" not in raw
    body = response.json()
    assert body["env_presence"]["SIGNAL_API_KEY"] is True
    assert "security" in body and "max_upload_bytes" in body["security"]


def test_cors_never_wildcard(client: TestClient) -> None:
    """The CORS allowlist never contains ``*``."""
    response = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_opsec_endpoints_never_500(client: TestClient) -> None:
    """Both OPSEC routes answer structurally even with no Tor available."""
    status = client.get("/api/opsec/status")
    assert status.status_code == 200
    assert isinstance(status.json(), dict)

    circuit = client.post("/api/opsec/newcircuit")
    assert circuit.status_code == 200
    assert isinstance(circuit.json(), dict)


# ── investigate ──────────────────────────────────────────────────────────────


def test_investigate_submit_and_get(client: TestClient) -> None:
    """Submit returns immediately with the legacy body; the poll carries state."""
    submitted = client.post("/api/investigate", json={"input": "example.com"})
    assert submitted.status_code == 200
    body = submitted.json()
    assert set(body) == {"inv_id", "case_id", "status"}
    assert body["status"] == "queued"
    assert len(body["inv_id"]) == 8

    record = client.get(f"/api/investigate/{body['inv_id']}").json()
    # Shape pinned by CONTRACTS.md §10.
    for key in ("inv_id", "case_id", "input", "input_type", "status", "steps", "report"):
        assert key in record, key


def test_investigate_unknown_is_404(client: TestClient) -> None:
    """An unknown id is a real 404 with the legacy ``error`` body."""
    response = client.get("/api/investigate/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "not found"


def test_investigate_rejects_overlong_input(client: TestClient) -> None:
    """Free-text input is capped, so a megabyte body cannot be accepted."""
    response = client.post("/api/investigate", json={"input": "a" * 9000})
    assert response.status_code == 422


def test_investigations_list_is_bare_array(client: TestClient) -> None:
    """`GET /api/investigations` returns a bare array (CasePanel indexes it)."""
    for target in ("alpha.example", "beta.example"):
        client.post("/api/investigate", json={"input": target})
    response = client.get("/api/investigations")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) >= 2
    assert {"inv_id", "status", "input"} <= set(body[0])


def test_investigations_pagination_headers(client: TestClient) -> None:
    """List pagination publishes the total and window in headers."""
    for i in range(3):
        client.post("/api/investigate", json={"input": f"page-{i}.example"})
    response = client.get("/api/investigations", params={"limit": 2, "offset": 0})
    assert response.status_code == 200
    assert len(response.json()) <= 2
    assert response.headers["X-Limit"] == "2"
    assert int(response.headers["X-Total-Count"]) >= 3


def test_investigations_filtered_by_case(client: TestClient) -> None:
    """`case_id` narrows the investigation list."""
    case = client.post("/api/cases", json={"name": "Filter Case"}).json()
    client.post("/api/investigate", json={"input": "in-case.example", "case_id": case["case_id"]})
    client.post("/api/investigate", json={"input": "other-case.example"})
    body = client.get("/api/investigations", params={"case_id": case["case_id"]}).json()
    assert body
    assert all(row["case_id"] == case["case_id"] for row in body)


def test_investigate_sync(client: TestClient) -> None:
    """`POST /api/investigate-sync` returns a finished record in one round trip."""
    response = client.post("/api/investigate-sync", json={"input": "sync-target.example"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("done", "error")
    if body["status"] == "done":
        assert body["report"]["summary"]
        assert body["input"] == "sync-target.example"
        assert isinstance(body["agents"], list)


def test_search(client: TestClient) -> None:
    """`POST /api/search` keeps main.py's response keys."""
    response = client.post("/api/search", json={"query": "site:example.com test"})
    assert response.status_code == 200
    body = response.json()
    for key in ("query", "agent", "status", "output", "confidence",
                "reasoning", "latency_s", "steps", "error"):
        assert key in body, key
    assert body["query"] == "site:example.com test"


def test_search_requires_query(client: TestClient) -> None:
    """An empty query is rejected by the schema."""
    assert client.post("/api/search", json={"query": ""}).status_code == 422


def test_name_search(client: TestClient) -> None:
    """`POST /api/name-search` returns the legacy queued envelope."""
    response = client.post("/api/name-search", json={"name": "Ada Lovelace"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["name"] == "Ada Lovelace"
    assert len(body["inv_id"]) == 8


def test_photo_search_accepts_png_and_cleans_up(client: TestClient) -> None:
    """A valid PNG is queued, and its temp directory is removed after the run."""
    response = client.post(
        "/api/photo-search",
        files={"file": ("face.png", PNG_1PX, "image/png")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["input_type"] == "photo"
    assert body["filename"] == "face.png"  # sanitised, not the raw header

    record = client.get(f"/api/investigate/{body['inv_id']}").json()
    media_path = record.get("media_path") or ""
    for _ in range(150):
        record = client.get(f"/api/investigate/{body['inv_id']}").json()
        if record.get("status") in ("done", "error"):
            break
        __import__("time").sleep(0.2)
    if media_path:
        # The leak main.py had: this must not survive the run.
        assert not os.path.exists(media_path), media_path
        assert not os.path.exists(os.path.dirname(media_path)), media_path


def test_photo_search_rejects_non_image(client: TestClient) -> None:
    """A non-image upload is refused before any file is written."""
    response = client.post(
        "/api/photo-search",
        files={"file": ("evil.sh", b"#!/bin/sh\nrm -rf /", "application/x-sh")},
    )
    assert response.status_code == 415
    assert "unsupported media type" in json.dumps(response.json())


def test_photo_search_rejects_disguised_non_image(client: TestClient) -> None:
    """A PNG content-type on a shell script is caught by the magic-byte gate."""
    response = client.post(
        "/api/photo-search",
        files={"file": ("sneaky.png", b"#!/bin/sh\n", "image/png")},
    )
    assert response.status_code == 415


def test_photo_search_sanitises_traversal_filename(client: TestClient) -> None:
    """A traversal filename cannot escape, and never leaks onto disk."""
    response = client.post(
        "/api/photo-search",
        files={"file": ("../../../etc/passwd.png", PNG_1PX, "image/png")},
    )
    assert response.status_code == 200, response.text
    assert "/" not in response.json()["filename"]
    assert ".." not in response.json()["filename"]


def test_compare_model_arena(client: TestClient) -> None:
    """`POST /api/compare` races models and returns a computed verdict."""
    response = client.post("/api/compare", json={
        "prompt": "example.com",
        "models": ["model-a", "model-b"],
    })
    assert response.status_code == 200
    body = response.json()
    assert len(body["models"]) == 2
    for entry in body["models"]:
        for key in ("model", "output", "latency_s", "prompt_tokens",
                    "completion_tokens", "total_tokens", "error", "ok"):
            assert key in entry, key
        assert entry["latency_s"] >= 0
    verdict = body["verdict"]
    assert verdict["winner"] in (None, "model-a", "model-b")
    assert verdict["ok_count"] + verdict["error_count"] == 2


def test_compare_rejects_unknown_field(client: TestClient) -> None:
    """Unknown fields are rejected — a typo must fail loudly, not be dropped."""
    assert client.post("/api/compare", json={
        "prompt": "x", "task": "rm -rf"}).status_code == 422


def test_compare_rejects_empty_prompt(client: TestClient) -> None:
    """An empty subject is rejected by the schema."""
    assert client.post("/api/compare", json={"prompt": ""}).status_code == 422


# ── cases ────────────────────────────────────────────────────────────────────


def test_case_crud_round_trip(client: TestClient) -> None:
    """Create -> list -> get -> patch -> delete, in order."""
    created = client.post("/api/cases", json={
        "name": "Operation Nightfall", "target": "example.com",
        "tags": ["osint", "urgent"], "notes": "initial notes",
    })
    assert created.status_code == 200
    case = created.json()
    case_id = case["case_id"]
    assert case["name"] == "Operation Nightfall"
    assert case["tags"] == ["osint", "urgent"]

    listing = client.get("/api/cases").json()
    assert listing["total"] >= 1
    assert any(row["case_id"] == case_id for row in listing["items"])

    fetched = client.get(f"/api/cases/{case_id}").json()
    assert fetched["case_id"] == case_id
    assert fetched["investigation_count"] == 0

    patched = client.patch(f"/api/cases/{case_id}", json={"notes": "updated"})
    assert patched.status_code == 200
    assert patched.json()["notes"] == "updated"
    # Untouched fields survive a partial patch.
    assert patched.json()["name"] == "Operation Nightfall"

    deleted = client.delete(f"/api/cases/{case_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/api/cases/{case_id}").status_code == 404
    assert client.delete(f"/api/cases/{case_id}").status_code == 404


def test_cases_list_pagination(client: TestClient) -> None:
    """`GET /api/cases` honours limit/offset and reports the total."""
    for i in range(4):
        client.post("/api/cases", json={"name": f"case-{i}"})
    page = client.get("/api/cases", params={"limit": 2}).json()
    assert len(page["items"]) == 2
    assert page["limit"] == 2
    assert page["total"] >= 4
    assert page["offset"] == 0

    second = client.get("/api/cases", params={"limit": 2, "offset": 2}).json()
    first_ids = {row["case_id"] for row in page["items"]}
    second_ids = {row["case_id"] for row in second["items"]}
    assert not (first_ids & second_ids)


def test_case_stats_entities_timeline(client: TestClient, done_inv: dict) -> None:
    """Stats, entities and timeline all derive from the finished run."""
    case_id = done_inv["case_id"]

    stats = client.get(f"/api/cases/{case_id}/stats").json()
    assert stats["case_id"] == case_id
    assert stats["investigation_count"] >= 1
    assert "entity_count" in stats and "signal_count" in stats
    assert "agent_count" in stats and "entity_types" in stats

    entities = client.get(f"/api/cases/{case_id}/entities").json()
    assert isinstance(entities, list)
    for entity in entities:
        assert "id" in entity and "type" in entity

    timeline = client.get(f"/api/cases/{case_id}/timeline").json()
    assert isinstance(timeline, list)
    for event in timeline:
        assert "id" in event and "date" in event and "label" in event


def test_case_entities_type_filter(client: TestClient, done_inv: dict) -> None:
    """The entity-type filter only returns that type."""
    case_id = done_inv["case_id"]
    response = client.get(f"/api/cases/{case_id}/entities", params={"type": "email"})
    assert response.status_code == 200
    body = response.json()
    assert all(row["type"] == "email" for row in body)


def test_case_routes_404_for_unknown_case(client: TestClient) -> None:
    """Every case sub-route 404s for an unknown case rather than 500ing."""
    assert client.get("/api/cases/nope").status_code == 404
    assert client.get("/api/cases/nope/stats").status_code == 404
    assert client.get("/api/cases/nope/entities").status_code == 404
    assert client.get("/api/cases/nope/timeline").status_code == 404
    assert client.post("/api/cases/nope/export").status_code == 404
    assert client.patch("/api/cases/nope", json={"name": "x"}).status_code == 404


def test_case_create_rejects_oversized_name(client: TestClient) -> None:
    """Case names are length-capped.

    The cap is whichever is tighter: the model's own field bound or the
    model-wide ``MAX_INPUT_LENGTH``, since both constraints apply.
    """
    field = CaseCreate.model_fields["name"]
    limits = [m.max_length for m in field.metadata
              if isinstance(m, type(field.metadata[0])) and getattr(m, "max_length", None)]
    assert limits, "CaseCreate.name must declare a max_length"
    assert client.post("/api/cases", json={"name": "x" * (min(limits) + 1)}).status_code == 422


def test_case_export_zip_contents(client: TestClient, done_inv: dict) -> None:
    """`POST /api/cases/{id}/export` streams a ZIP with the documented members."""
    case_id = done_inv["case_id"]
    response = client.post(f"/api/cases/{case_id}/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="case-' in response.headers["content-disposition"]

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert {"case.json", "entities.json", "timeline.json", "graph.json",
            "investigations.json", "README.md"} <= names
    assert any(name.startswith("reports/") for name in names)

    case_payload = json.loads(archive.read("case.json"))
    assert case_payload["case"]["case_id"] == case_id
    assert isinstance(json.loads(archive.read("entities.json")), list)
    assert isinstance(json.loads(archive.read("timeline.json")), list)
    graph = json.loads(archive.read("graph.json"))
    assert "nodes" in graph and "edges" in graph
    assert archive.testzip() is None


def test_case_export_leaves_no_temp_files(client: TestClient, done_inv: dict) -> None:
    """The ZIP is built in memory — nothing lands in the working directory."""
    before = set(os.listdir(BACKEND_ROOT))
    client.post(f"/api/cases/{done_inv['case_id']}/export")
    assert set(os.listdir(BACKEND_ROOT)) == before


# ── agents ───────────────────────────────────────────────────────────────────


def test_agents_registry(client: TestClient) -> None:
    """`GET /api/agents` returns the full registry with the documented fields."""
    response = client.get("/api/agents")
    assert response.status_code == 200
    body = response.json()
    items = body["items"]
    assert body["total"] == len(items)
    assert len(items) >= 14  # spec: all agents must survive the refactor

    names = {item["name"] for item in items}
    assert {"APEX", "SCOUT", "NEXUS", "IRIS", "QUILL", "VAULT"} <= names

    by_name = {item["name"]: item for item in items}
    for key in ("role", "icon", "description", "models", "tier"):
        assert key in by_name["SCOUT"], key
    assert by_name["APEX"]["tier"] == "supervisor"
    assert by_name["NEXUS"]["tier"] == "correlation"
    assert by_name["SCOUT"]["tier"] == "primary"


def test_agents_registry_graph(client: TestClient) -> None:
    """The routing DAG exposes nodes, edges and the input-type vocabulary."""
    response = client.get("/api/agents/registry/graph")
    assert response.status_code == 200
    body = response.json()

    assert body["nodes"] and body["edges"]
    assert "input_types" in body and "person_name" in body["input_types"]

    node_ids = {node["id"] for node in body["nodes"]}
    assert "APEX" in node_ids
    assert any(node["kind"] == "input_type" for node in body["nodes"])

    # Every edge must reference real nodes, or the UI renders dangling arrows.
    for edge in body["edges"]:
        assert edge["source"] in node_ids, edge
        assert edge["target"] in node_ids, edge


def test_agents_graph_route_not_shadowed(client: TestClient) -> None:
    """`/agents/registry/graph` is not captured by the `/agents/{name}` route."""
    assert client.get("/api/agents/registry/graph").status_code == 200
    assert client.get("/api/agents/SCOUT").status_code == 200


def test_agent_descriptor(client: TestClient) -> None:
    """A single descriptor resolves case-insensitively; unknown names 404."""
    upper = client.get("/api/agents/SCOUT").json()
    lower = client.get("/api/agents/scout").json()
    assert upper["name"] == lower["name"] == "SCOUT"

    missing = client.get("/api/agents/NOPE")
    assert missing.status_code == 404
    assert "known_agents" in missing.json()["detail"]


def test_agent_run_stub(client: TestClient) -> None:
    """A standalone run returns the documented envelope."""
    response = client.post("/api/agents/TERRA/run", json={"input": "8.8.8.8"})
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "TERRA"
    assert body["status"] in ("done", "error", "partial", "skipped")
    assert "latency_s" in body
    if body["status"] != "error":
        assert body["result"]["agent"] == "TERRA"


def test_agent_run_unknown_404(client: TestClient) -> None:
    """Running an unregistered agent is a 404, not a 500."""
    response = client.post("/api/agents/NOPE/run", json={"input": "x"})
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown agent"


def test_agent_run_rejects_empty_input(client: TestClient) -> None:
    """An empty run input is rejected by the schema."""
    assert client.post("/api/agents/SCOUT/run", json={"input": ""}).status_code == 422


# ── reports ──────────────────────────────────────────────────────────────────


def test_report_markdown(client: TestClient, done_inv: dict) -> None:
    """`format=markdown` returns real markdown, not an empty body."""
    response = client.get(f"/api/investigate/{done_inv['inv_id']}/report",
                          params={"format": "markdown"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    body = response.text
    assert body.strip()
    # QUILL renders the brief; the investigation is identifiable by the case id
    # it was filed under, which QUILL does emit.
    assert done_inv["case_id"] in body


def test_report_json(client: TestClient, done_inv: dict) -> None:
    """`format=json` returns the structured report payload."""
    response = client.get(f"/api/investigate/{done_inv['inv_id']}/report",
                          params={"format": "json"})
    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "json"
    assert body["inv_id"] == done_inv["inv_id"]
    assert "generated_at" in body
    assert "report" in body


def test_report_pdf(client: TestClient, done_inv: dict) -> None:
    """`format=pdf` returns a structurally valid, non-trivial PDF."""
    response = client.get(f"/api/investigate/{done_inv['inv_id']}/report",
                          params={"format": "pdf"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]

    payload = response.content
    assert payload.startswith(b"%PDF-")
    assert payload.rstrip().endswith(b"%%EOF")
    assert b"xref" in payload and b"trailer" in payload
    assert len(payload) > 500


def test_report_defaults_to_markdown(client: TestClient, done_inv: dict) -> None:
    """No `format` means markdown."""
    response = client.get(f"/api/investigate/{done_inv['inv_id']}/report")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")


def test_report_rejects_bad_format(client: TestClient, done_inv: dict) -> None:
    """An unsupported format is a 400 listing the supported ones."""
    response = client.get(f"/api/investigate/{done_inv['inv_id']}/report",
                          params={"format": "docx"})
    assert response.status_code == 400
    assert set(response.json()["detail"]["supported"]) == {"markdown", "pdf", "json"}


def test_report_unknown_inv_404(client: TestClient) -> None:
    """An unknown investigation is a 404 on the report route too."""
    assert client.get("/api/investigate/nope/report").status_code == 404
    assert client.get("/api/investigate/nope/export").status_code == 404


def test_report_conflicts_while_running(client: TestClient) -> None:
    """A still-running investigation yields 409, not a half-written report."""
    submitted = client.post("/api/investigate", json={"input": "slow.example"}).json()
    response = client.get(f"/api/investigate/{submitted['inv_id']}/report")
    assert response.status_code in (200, 409)
    if response.status_code == 409:
        assert response.json()["detail"]["error"] == "investigation still running"


def test_export_artefact(client: TestClient, done_inv: dict) -> None:
    """`/export` returns the artefact as a download."""
    response = client.get(f"/api/investigate/{done_inv['inv_id']}/export",
                          params={"format": "pdf"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")


def test_export_bundle_zip(client: TestClient, done_inv: dict) -> None:
    """`bundle=true` returns a ZIP carrying every report format."""
    response = client.get(f"/api/investigate/{done_inv['inv_id']}/export",
                          params={"bundle": "true"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert {"report.md", "report.json", "report.pdf", "agents.json",
            "steps.json"} <= names
    assert archive.read("report.pdf").startswith(b"%PDF-")
    assert archive.testzip() is None


def test_export_markdown_leaves_no_files(client: TestClient, done_inv: dict) -> None:
    """No artefact is written to disk on the export path."""
    before = set(os.listdir(BACKEND_ROOT))
    client.get(f"/api/investigate/{done_inv['inv_id']}/export",
               params={"format": "markdown"})
    assert set(os.listdir(BACKEND_ROOT)) == before


# ── rate limiting ────────────────────────────────────────────────────────────


def test_rate_limit_headers_present(client: TestClient) -> None:
    """Every guarded route publishes the X-RateLimit-* headers."""
    for path in ("/api/health", "/api/metrics", "/api/agents", "/api/cases",
                 "/api/investigations"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "X-RateLimit-Limit" in response.headers, path
        assert "X-RateLimit-Remaining" in response.headers, path
        assert "X-RateLimit-Reset" in response.headers, path


def test_rate_limit_scope_routing() -> None:
    """Routes map onto WS-SEC's named scopes, and reads stay cheap.

    The split matters in practice: the dashboard polls an investigation and the
    agent registry continuously while a pipeline streams, and charging those
    polls to the 6-rpm `investigate` budget would 429 a client that is only
    watching.
    """
    from api.ops import scope_for

    # Work initiators pay the expensive budget.
    assert scope_for("/api/investigate", "POST") == "investigate"
    assert scope_for("/api/investigate-sync", "POST") == "investigate"
    assert scope_for("/api/photo-search", "POST") == "investigate"
    assert scope_for("/api/name-search", "POST") == "investigate"
    assert scope_for("/api/agents/TERRA/run", "POST") == "investigate"
    assert scope_for("/api/search", "POST") == "search"
    assert scope_for("/api/compare", "POST") == "search"
    assert scope_for("/api/opsec/status") == "opsec"

    # Reads are cheap, including the live-poll paths.
    assert scope_for("/api/investigate/abc123", "GET") == "read"
    assert scope_for("/api/investigate/abc123/report", "GET") == "read"
    assert scope_for("/api/investigate/abc123/export", "GET") == "read"
    assert scope_for("/api/agents", "GET") == "read"
    assert scope_for("/api/agents/registry/graph", "GET") == "read"
    assert scope_for("/api/agents/SCOUT", "GET") == "read"
    assert scope_for("/api/cases/xyz/entities", "GET") == "read"
    assert scope_for("/api/health", "GET") == "default"


def test_rate_limit_remaining_decrements(client: TestClient) -> None:
    """The remaining-budget header counts down across calls."""
    first = client.get("/api/health").headers.get("X-RateLimit-Remaining")
    second = client.get("/api/health").headers.get("X-RateLimit-Remaining")
    assert first is not None and second is not None
    assert int(second) <= int(first)


def test_rate_limit_enforced_when_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exhausting a burst returns 429 with a Retry-After header."""
    from api import ops

    # WS-SEC reads per-scope overrides first, then the global pair.
    monkeypatch.setenv("RATE_LIMIT_RPM", "3")
    monkeypatch.setenv("RATE_LIMIT_BURST", "3")
    monkeypatch.setenv("RATE_LIMIT_SCOPE_RPM.default", "3")
    monkeypatch.setenv("RATE_LIMIT_BURST.default", "3")

    import auth as auth_mod
    import rate_limit as rl
    import runtime as runtime_mod

    previous_auth_enabled = auth_mod.auth_enabled
    auth_mod.auth_enabled = lambda: False

    async def _install() -> None:
        import store as sm

        memory = sm.MemoryStore(max_rows=100)
        await memory.start()
        await sm.set_store(memory)
        runtime_mod._STORE = memory
        runtime_mod._STORE_READY = True

    import asyncio as _asyncio

    _asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_install())

    # Re-impose a tiny budget on the `investigate` scope for this test only.
    # `rate_limit.reset_all()` only refills existing buckets — it does not
    # rebuild them — so the limit captured when the limiter was first created
    # (with this module's generous defaults) would survive. Clearing the
    # registry forces the next `get_limiter` call to re-read the env.
    monkeypatch.setenv("RATE_LIMIT_SCOPE_RPM.investigate", "6")
    monkeypatch.setenv("RATE_LIMIT_BURST.investigate", "2")
    rl._BUCKETS["investigate"].clear()
    ops._BUCKETS.clear()
    app = build_app()
    ops.install_cors(app)
    try:
        with TestClient(app) as client:
            codes = [client.post("/api/investigate",
                                 json={"input": f"rl-{i}.example"}).status_code
                     for i in range(8)]
            assert 429 in codes, codes
            limited = client.post("/api/investigate", json={"input": "rl-x.example"})
            assert limited.status_code == 429
            assert "Retry-After" in limited.headers
            # A cheap read scope is unaffected by the investigate budget.
            assert client.get("/api/health").status_code == 200
    finally:
        auth_mod.auth_enabled = previous_auth_enabled
        rl.reset_all()
        ops._BUCKETS.clear()


# ── websocket ────────────────────────────────────────────────────────────────


def test_websocket_replays_done_for_finished_run(client: TestClient, done_inv: dict) -> None:
    """Subscribing to a finished run replays one `done` event (backwards compat)."""
    with client.websocket_connect(f"/ws/pipeline/{done_inv['inv_id']}") as ws:
        event = ws.receive_json()
        assert event["type"] == "done"
        assert event["inv_id"] == done_inv["inv_id"]
        assert "report" in event


def test_websocket_ping_pong(client: TestClient, done_inv: dict) -> None:
    """A client ping is echoed with a nonce, keeping intermediaries awake."""
    with client.websocket_connect(f"/ws/pipeline/{done_inv['inv_id']}") as ws:
        ws.receive_json()  # the replayed done
        ws.send_text(json.dumps({"type": "ping", "nonce": 7}))
        # The first frame after `done` is our echo.
        for _ in range(3):
            event = ws.receive_json()
            if event.get("type") == "ping":
                assert event.get("nonce") == 7
                return
        pytest.fail("no ping echo received")


def test_websocket_unknown_inv_stays_open(client: TestClient) -> None:
    """Connecting to an unknown run does not crash the server."""
    with client.websocket_connect("/ws/pipeline/unknown-id") as ws:
        ws.send_text(json.dumps({"type": "ping", "nonce": "abc"}))


def test_websocket_origin_rejected_when_allowlist_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`WS_ALLOWED_ORIGINS` is enforced: a foreign origin is refused."""
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "https://allowed.example")

    import runtime as runtime_mod

    async def _install() -> None:
        import store as sm

        memory = sm.MemoryStore(max_rows=100)
        await memory.start()
        await sm.set_store(memory)
        runtime_mod._STORE = memory
        runtime_mod._STORE_READY = True

    import asyncio as _asyncio

    _asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_install())

    app = build_app()
    with TestClient(app) as client:
        from starlette.websockets import WebSocketDisconnect

        try:
            with client.websocket_connect(
                "/ws/pipeline/abc",
                headers={"Origin": "https://evil.example"},
            ):
                pytest.fail("connection should have been refused")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008

        # The allowed origin still connects.
        with client.websocket_connect(
            "/ws/pipeline/abc", headers={"Origin": "https://allowed.example"}
        ) as ws:
            ws.send_text(json.dumps({"type": "ping"}))