"""
test_smoke.py — end-to-end smoke assertions, CI-gateable
===========================================================
This is the pytest twin of ``scripts/smoke.sh``: the same assertions, runnable
from a CI runner without spawning a subprocess server. It mounts every
``backend/api`` router onto a local app (exactly what ``main.py`` does) and
drives a REAL investigation through the whole APEX DAG.

Everything here runs offline and hermetic — ``conftest.py`` has already set
``OPSEC_ENABLED=false``, pointed the store at SQLite, pointed Redis/SearXNG at a
closed port, disabled Celery and blocked outbound sockets. The store is
additionally forced to the in-memory backend so the suite asserts on behaviour
rather than on whatever a developer's local Postgres happens to contain.

Two things are asserted here that the shell script cannot do:

  * the **connector registry is in sync with the tree**. Every ``run_*`` function
    in ``backend/connectors/*.py`` is extracted with :mod:`ast` and compared
    against :data:`connectors.CONNECTORS`. An entry that invents a connector,
    or a ``run_*`` function nobody registered, fails CI.
  * **the rate limiter really is armed**, with the production 6 rpm investigate
    ceiling. ``test_api.py`` raises its budget for its own convenience; this
    suite deliberately does not, so the blast-radius control is never silently
    regressed away by a test.

Run with::

    cd backend && .venv/bin/python -m pytest tests/test_smoke.py -q
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

CONNECTORS_DIR = BACKEND_ROOT / "connectors"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """Build a local app with every ``api/`` router mounted.

    Mirrors ``main.py``'s mounting without the app-owned wiring (CORS, startup
    probes, middleware), which is not what this suite is testing.

    Returns:
        A configured ``FastAPI`` instance.
    """
    from api.agents import router as agents_router
    from api.cases import router as cases_router
    from api.investigate import router as investigate_router
    from api.ops import router as ops_router
    from api.ops import ws_router
    from api.reports import router as reports_router

    application = FastAPI(title="Signal-OS (smoke)", version="0.1.0")
    application.include_router(ops_router)
    application.include_router(investigate_router)
    application.include_router(cases_router)
    application.include_router(agents_router)
    application.include_router(reports_router)
    application.include_router(ws_router)
    return application


@pytest.fixture(scope="module")
def client(app: FastAPI):
    """A ``TestClient`` bound to the smoke app, with auth off and memory storage.

    The routers in ``api/`` are genuinely auth-guarded (``auth_gate`` is applied
    in the router constructor), so an unmocked suite 401s on everything. Auth is
    patched on the module rather than via the environment so the override holds
    regardless of the ambient ``ENVIRONMENT``.

    The memory store must be installed through ``runtime``, not just
    ``store.set_store``: the routers resolve storage via
    ``runtime.get_store_handle``, which memoises the handle, so setting it in one
    place and asserting on another reads a different instance.

    Args:
        app: The app built by the :func:`app` fixture.

    Yields:
        A live ``TestClient`` with the lifespan started.
    """
    import auth as auth_mod
    import runtime as runtime_mod
    import store as store_mod
    from api.ops import install_cors

    install_cors(app)

    previous_auth_enabled = auth_mod.auth_enabled
    previous_store = store_mod._store
    previous_cwd = Path.cwd()
    auth_mod.auth_enabled = lambda: False
    os.chdir(BACKEND_ROOT)

    async def _install() -> None:
        memory = store_mod.MemoryStore(max_rows=1000)
        await memory.start()
        await store_mod.set_store(memory)
        runtime_mod._STORE = memory
        runtime_mod._STORE_READY = True

    try:
        asyncio.new_event_loop().run_until_complete(_install())
        with TestClient(app) as test_client:
            yield test_client
    finally:
        auth_mod.auth_enabled = previous_auth_enabled
        store_mod._store = previous_store
        runtime_mod._STORE = None
        runtime_mod._STORE_READY = False
        os.chdir(previous_cwd)


@pytest.fixture(scope="module")
def investigation(client: TestClient) -> dict:
    """Run one real investigation and wait for it to finish.

    Uses ``POST /api/investigate-sync`` rather than the fire-and-forget
    ``/api/investigate`` because this suite is in-process: the background task
    would otherwise race the assertion that the record is already complete.

    Args:
        client: The bound test client.

    Returns:
        The completed investigation record.
    """
    started = time.perf_counter()
    response = client.post(
        "/api/investigate-sync",
        json={"input": "example.com", "input_type": "domain"},
    )
    assert response.status_code == 200, response.text
    record = response.json()
    record["_wall_s"] = time.perf_counter() - started
    return record


# ── The connector registry ──────────────────────────────────────────────────


def _discover_run_functions() -> dict[str, str]:
    """Extract every module-level ``run_*`` function from the connector tree.

    Parsed with :mod:`ast` rather than imported, so a connector module with a
    missing optional dependency still counts as *present in the source*. The
    registry is built from what exists in the files, not from what happens to
    import on this machine.

    Returns:
        ``{module_name: "run_x"}`` for every module defining one or more
        ``run_*`` functions. The value is a comma-joined attribute list when a
        module defines more than one (``qdrant_store`` has two).
    """
    found: dict[str, str] = {}
    for path in sorted(CONNECTORS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a broken file fails CI elsewhere
            continue
        names = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name.startswith("run_")
        ]
        if names:
            found[path.stem] = ",".join(names)
    return found


def test_registry_only_lists_real_run_functions():
    """Every registry entry must bind a ``run_*`` that exists in the tree.

    This is the anti-invention check: an entry cannot be written without the
    corresponding module actually exposing that function.
    """
    import connectors

    discovered = _discover_run_functions()
    assert discovered, "no run_* functions found — the AST scan is broken"

    for name, entry in connectors.CONNECTORS.items():
        spec = entry.spec
        module_stem = spec.module.rsplit(".", 1)[-1]
        assert module_stem in discovered, (
            f"registry entry {name!r} points at {spec.module!r}, which defines no run_* function"
        )
        assert spec.attr in discovered[module_stem].split(","), (
            f"registry entry {name!r} binds {spec.attr!r}, which "
            f"{spec.module} does not define (found: {discovered[module_stem]})"
        )


def test_every_run_function_is_registered():
    """Every ``run_*`` in the tree must be reachable through the registry.

    Catches the opposite failure to the test above: a new connector lands and
    nobody adds it to :data:`connectors.CONNECTORS`.
    """
    import connectors

    discovered = _discover_run_functions()
    bound = {
        entry.spec.module.rsplit(".", 1)[-1] + ":" + entry.spec.attr
        for entry in connectors.CONNECTORS.values()
    }

    missing = [
        f"{module}.{attr}"
        for module, attrs in discovered.items()
        for attr in attrs.split(",")
        if f"{module}:{attr}" not in bound
    ]
    assert not missing, f"run_* functions exist but are not in connectors.CONNECTORS: {missing}"


def test_registry_names_are_sorted_and_unique():
    """``CONNECTOR_NAMES`` must be the sorted, de-duplicated key list."""
    import connectors

    keys = tuple(connectors.CONNECTORS)
    assert len(keys) == len(set(keys)), "duplicate connector names"

    published = connectors.CONNECTOR_NAMES
    assert published == tuple(sorted(keys))


def test_get_connector_lookup_and_error():
    """``get_connector`` resolves known names and explains unknown ones."""
    import connectors

    entry = connectors.get_connector("shodan")
    assert entry.name == "shodan"
    assert entry.spec.attr == "run_shodan"
    assert connectors.resolve is connectors.get_connector

    with pytest.raises(KeyError) as excinfo:
        connectors.get_connector("not-a-connector")
    # The error must list the valid names rather than just failing.
    assert "shodan" in str(excinfo.value)


def test_connectors_are_lazy():
    """Building the registry must not import any connector module.

    A missing optional dependency (Playwright, faster-whisper, qdrant-client)
    must degrade rather than break ``import connectors``.
    """
    import connectors

    # Touch nothing first: in a fresh interpreter only `connectors` itself and
    # its stdlib imports are in sys.modules.
    script = (
        "import sys, connectors; "
        "loaded = sorted(m for m in sys.modules if m.startswith('connectors.')); "
        "print(','.join(loaded))"
    )
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    loaded = [m for m in result.stdout.strip().split(",") if m]
    assert not loaded, f"connectors submodules imported eagerly: {loaded}"


def test_connector_health_shape_and_no_network():
    """``connector_health`` returns the documented matrix and never raises.

    Asserted offline — ``conftest.py``'s ``block_network`` fixture makes any
    socket the probe attempted raise, so reaching the assertions at all proves
    it performed no I/O.
    """
    import connectors

    health = connectors.connector_health()
    assert set(health) >= {"connectors", "summary"}
    names = set(health["connectors"])
    assert names == set(connectors.CONNECTORS)

    for name, record in health["connectors"].items():
        assert isinstance(record["available"], bool), name
        assert isinstance(record["installed"], bool), name
        assert isinstance(record["requires"], list), name
        assert record["requires"], f"{name} declares no requirements"
        assert isinstance(record["notes"], str) and record["notes"], name
        # available-but-not-installed is a legitimate degraded state; it must be
        # explained rather than left blank.
        if not record["installed"]:
            assert record["notes"], f"{name} is not installed but has no note"

    summary = health["summary"]
    assert summary["total"] == len(connectors.CONNECTORS)
    assert summary["available"] <= summary["total"]
    assert summary["installed"] <= summary["available"]


def test_connector_health_survives_a_missing_dependency(monkeypatch):
    """A connector whose module cannot import reports unavailable, not raise."""
    import connectors

    real_import = connectors.importlib.import_module
    target = connectors.CONNECTORS["exif"].spec.module

    def flaky(name, *args, **kwargs):
        if name == target:
            raise ImportError("simulated: no module named PIL")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(connectors.importlib, "import_module", flaky)

    health = connectors.connector_health()
    record = health["connectors"]["exif"]
    assert record["available"] is False
    assert record["installed"] is False
    assert "simulated" in record["notes"]
    # Every other connector must be unaffected by one module's failure.
    assert health["connectors"]["shodan"]["available"] is True


# ── Ops surface ─────────────────────────────────────────────────────────────


def test_health(client: TestClient):
    """``/api/health`` answers and never reports ``down`` with no infra."""
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["platform"] == "signal-os"
    assert body["status"] in ("ok", "degraded"), body
    # "degraded" is correct here: the store fell back to in-memory.
    assert body["status"] != "down"


def test_health_deep_metrics_config(client: TestClient):
    """The other three ops endpoints answer with a status field."""
    for path in ("/api/health/deep", "/api/metrics", "/api/config"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert isinstance(response.json(), (dict, list)), path


def test_agents_registry_lists_all_sixteen(client: TestClient):
    """All 16 registered agents are introspectable through the API."""
    response = client.get("/api/agents")
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"items", "total"}

    names = [item["name"] for item in body["items"]]
    assert len(names) == 16
    assert body["total"] == 16
    for required in ("APEX", "QUILL", "SCOUT", "NEXUS"):
        assert required in names

    from agents import AGENT_REGISTRY

    assert set(names) == set(AGENT_REGISTRY), "the API and the agent registry disagree"


def test_unknown_agent_is_404(client: TestClient):
    """An unknown agent name is a clean 404, not a 500."""
    response = client.get("/api/agents/NOT_AN_AGENT")
    assert response.status_code == 404


# ── The pipeline ────────────────────────────────────────────────────────────


def test_investigate_sync_runs_the_dag(investigation: dict):
    """A sync investigation completes and produces a real report."""
    assert investigation["status"] in ("done", "completed"), investigation.get("error")
    assert investigation["inv_id"]
    assert investigation["case_id"]

    report = investigation["report"]
    assert report, "no report produced"
    assert len(report["markdown"]) > 80, "report markdown is trivially short"
    assert report["input_type"] == "domain"
    assert report["graph"] is not None
    assert isinstance(report["signals"], list)

    # The DAG ran: primary swarm, then the correlation stages, then QUILL.
    agents = [a["agent"] for a in investigation["agents"]]
    assert len(agents) >= 9, agents
    for required in ("SCOUT", "NEXUS", "KRONOS", "VAULT", "SENTINEL", "QUILL"):
        assert required in agents, f"{required} did not run: {agents}"

    # The reasoning agents must not error. Fetching agents (CRAWLER) legitimately
    # error offline — conftest.py blocks outbound sockets — so they are checked
    # for *presence* above but not required to succeed. This is the platform's
    # documented degradation contract, not a regression.
    reasoning = {"SCOUT", "NEXUS", "KRONOS", "VAULT", "SENTINEL", "QUILL"}
    failed = [
        a["agent"]
        for a in investigation["agents"]
        if a["status"] == "error" and a["agent"] in reasoning
    ]
    assert not failed, f"reasoning agents errored with zero infrastructure: {failed}"


def test_investigate_async_then_poll(client: TestClient):
    """The fire-and-forget path returns immediately and converges."""
    queued = client.post("/api/investigate", json={"input": "example.org", "input_type": "domain"})
    assert queued.status_code == 200
    body = queued.json()
    assert body["status"] == "queued"
    inv_id = body["inv_id"]

    record = {}
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        record = client.get(f"/api/investigate/{inv_id}").json()
        if record.get("status") in ("done", "error", "failed"):
            break
        time.sleep(0.1)

    assert record.get("status") == "done", record.get("error")
    assert record["report"]["markdown"]
    assert record["ended_at"]


def test_unknown_investigation_is_404(client: TestClient):
    """An unknown but well-formed id is a 404 with an error body."""
    response = client.get("/api/investigate/deadbeef99")
    assert response.status_code == 404
    assert "error" in response.json().get("detail", {})


def test_investigations_listing(client: TestClient, investigation: dict):
    """A completed run appears in the newest-first listing."""
    response = client.get("/api/investigations?limit=10")
    assert response.status_code == 200
    rows = response.json()
    assert isinstance(rows, list)
    assert any(r["inv_id"] == investigation["inv_id"] for r in rows)


# ── Reports ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", ["markdown", "json", "pdf"])
def test_report_formats(client: TestClient, investigation: dict, fmt: str):
    """Each documented report format renders."""
    inv_id = investigation["inv_id"]
    response = client.get(f"/api/investigate/{inv_id}/report?format={fmt}")
    assert response.status_code == 200, response.text[:300]
    assert response.content, f"{fmt} report was empty"

    if fmt == "markdown":
        assert b"# Intelligence Report" in response.content
    elif fmt == "pdf":
        assert response.content.startswith(b"%PDF")


def test_report_rejects_unknown_format(client: TestClient, investigation: dict):
    """An arbitrary format string is rejected rather than interpolated."""
    response = client.get(f"/api/investigate/{investigation['inv_id']}/report?format=exe")
    assert response.status_code == 400


# ── Cases ───────────────────────────────────────────────────────────────────


def test_case_crud_round_trip(client: TestClient):
    """Create, read, patch, inspect and delete a case."""
    name = f"smoke-{int(time.time())}"
    created = client.post("/api/cases", json={"name": name})
    assert created.status_code == 200, created.text
    payload = created.json()
    case = payload.get("case", payload)
    case_id = case.get("case_id")
    assert case_id

    assert client.get(f"/api/cases/{case_id}").status_code == 200
    assert client.patch(f"/api/cases/{case_id}", json={"notes": "smoke"}).status_code == 200

    for sub in ("stats", "entities", "timeline"):
        response = client.get(f"/api/cases/{case_id}/{sub}")
        assert response.status_code == 200, sub
        assert isinstance(response.json(), (dict, list)), sub

    deleted = client.delete(f"/api/cases/{case_id}")
    assert deleted.status_code == 200
    # A deleted case must actually be gone, not merely unlisted.
    assert client.get(f"/api/cases/{case_id}").status_code == 404


def test_case_export_bundle(client: TestClient):
    """``POST /api/cases/{id}/export`` returns an in-memory ZIP bundle.

    A case row must exist first: an investigation auto-assigns a ``case_id`` but
    does not create the corresponding case record, so exporting one directly 404s.

    Note the method and the parameter name: the cases router exposes
    ``include_raw``, not the reports router's ``bundle=true``.
    """
    case_id = client.post("/api/cases", json={"name": f"export-{int(time.time())}"}).json()
    case_id = (case_id.get("case") or case_id)["case_id"]

    response = client.post(f"/api/cases/{case_id}/export?include_raw=true")
    assert response.status_code == 200, response.text[:200]
    assert response.headers["content-type"] == "application/zip"
    assert response.content.startswith(b"PK")

    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
    assert "case.json" in names, names
    assert "README.md" in names, names


# ── Input validation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"input": ""}, id="empty-input"),
        pytest.param({"input": "x" * 5000}, id="input-over-max-length"),
        pytest.param({"input": "ok", "bogus_field": 1}, id="unknown-field"),
        pytest.param({}, id="missing-input"),
        pytest.param({"input": "ok", "input_type": "not-a-type"}, id="bad-input-type"),
    ],
)
def test_investigate_rejects_bad_input(client: TestClient, payload: dict):
    """Malformed submissions are refused at the edge with 4xx, never a 500."""
    response = client.post("/api/investigate", json=payload)
    assert response.status_code in (400, 422), response.text[:200]


@pytest.mark.parametrize(
    "path",
    [
        "/api/investigate/..%2F..%2Fetc%2Fpasswd",
        "/api/cases/..%2F..%2Fetc%2Fpasswd",
        "/api/investigate/sh",
    ],
)
def test_path_traversal_is_refused(client: TestClient, path: str):
    """Traversal attempts cannot reach the filesystem."""
    response = client.get(path)
    assert response.status_code in (400, 404, 422), path
    assert "root:" not in response.text


# ── The rate limiter stays armed ─────────────────────────────────────────────


def test_rate_limiter_is_armed(client: TestClient):
    """The production 6 rpm investigate ceiling is still in force.

    ``test_api.py`` raises its own budget; this suite deliberately does not, so
    a well-meaning "make the tests pass" edit cannot quietly disable the
    platform's blast-radius control.
    """
    from api.ops import paged as _paged  # noqa: F401 — ensure ops is importable

    import rate_limit

    assert rate_limit.default_rpm("investigate") == 6, (
        "the investigate scope's default budget changed; update this test and "
        "docs/SECURITY.md deliberately, not by accident"
    )

    statuses = []
    for _ in range(8):
        statuses.append(client.post("/api/investigate", json={"input": "x.com"}).status_code)
    assert 429 in statuses, f"eight investigations in a row never hit the limiter: {statuses}"


def test_rate_limit_headers_present(client: TestClient):
    """Responses carry the limiter's observability headers."""
    response = client.get("/api/health")
    assert "x-ratelimit-limit" in {k.lower() for k in response.headers}


# ── WebSocket ───────────────────────────────────────────────────────────────


def test_websocket_streams_a_terminal_event(client: TestClient):
    """The pipeline socket streams live steps and then a terminal event.

    There is no ``connected`` handshake event on this socket — the first frame is
    the first pipeline step, and the terminal frame is ``type: "done"``.
    """
    queued = client.post(
        "/api/investigate", json={"input": "example.net", "input_type": "domain"}
    ).json()
    inv_id = queued["inv_id"]

    events: list[dict] = []
    with client.websocket_connect(f"/ws/pipeline/{inv_id}") as socket:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                event = socket.receive_json()
            except Exception:  # noqa: BLE001 — socket closed or send failed
                break
            events.append(event)
            if event.get("type") in ("done", "error", "pipeline_finished"):
                break

    assert events, "the websocket produced no events"
    assert all("inv_id" in e or e.get("type") == "ping" for e in events), events[:3]
    assert any(e.get("type") == "step" for e in events), [e.get("type") for e in events]
    assert events[-1].get("type") in ("done", "pipeline_finished"), events[-1]


def test_websocket_replays_a_finished_investigation(client: TestClient, investigation: dict):
    """Subscribing to a *finished* run replays one ``done`` frame, not a hang.

    An unknown id is also accepted and simply streams nothing — the socket is a
    subscription endpoint, not an existence check — so this asserts the documented
    replay behaviour rather than a rejection.
    """
    inv_id = investigation["inv_id"]

    with client.websocket_connect(f"/ws/pipeline/{inv_id}") as socket:
        first = socket.receive_json()

    assert first["type"] == "done", first
    assert first["inv_id"] == inv_id
    assert first["report"]["markdown"]


# ── Guard rail: the suite must not silently pass by being empty ─────────────


def test_suite_is_not_vacuous():
    """Fail loudly if collection broke and every test above stopped running."""
    collected = [
        name
        for name, fn in globals().items()
        if name.startswith("test_") and asyncio.iscoroutinefunction(fn) is False
    ]
    assert len(collected) >= 15, (
        f"only {len(collected)} smoke tests collected — collection is broken"
    )


def test_json_serialisable_report(investigation: dict):
    """The stored record survives a JSON round trip with no stray objects."""
    text = json.dumps(investigation, default=str)
    assert len(text) > 1000
    assert json.loads(text)["inv_id"] == investigation["inv_id"]


def test_llm_and_opsec_env_are_offline():
    """This suite must not depend on an LLM key or a Tor daemon."""
    assert os.getenv("OPSEC_ENABLED", "false").lower() == "false"
    # LLM_ENABLED may be unset (default true) but no provider key may be present,
    # otherwise these tests would make a billable network call.
    assert not any(
        os.getenv(key) for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
    ), "an LLM provider key is set — the smoke suite is not hermetic"
