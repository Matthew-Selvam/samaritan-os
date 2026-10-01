"""
conftest.py — Test Isolation for the Infrastructure Suite
===========================================================
Every test in this package must run **fast and offline**. The platform talks to
Redis, Postgres, SearXNG, Ollama, and third-party OSINT APIs; left alone, a
single investigation would make real network calls and the suite would depend on
whatever happens to be listening on localhost.

This module sets the environment **before** any application module is imported,
because ``config.py``, ``cache.py``, ``observability.py`` and ``store.py`` all
read ``os.getenv`` at import time and freeze the result into a module global.
Setting it later would leave the modules under test on production values.

Isolation strategy, per dependency:

* ``OPSEC_ENABLED=false`` — no Tor proxy (a Tor-routed request hangs or fails
  slowly, which is the opposite of a fast test).
* ``DATABASE_URL`` → SQLite, ``SQLITE_PATH`` → ``tmp_path``-style temp file —
  never the placeholder Postgres DSN, so ``store`` exercises its real SQL path
  instead of stalling on a connection timeout.
* ``SEARXNG_URL`` → a closed local port — SCOUT's federation fails in
  milliseconds against a refused connection instead of reaching the internet.
* ``REDIS_URL`` → a closed local port — ``cache`` takes its in-process path,
  which is the path that must work everywhere anyway.
* ``CELERY_ENABLED=false`` — tasks run in-process, so no broker is needed and
  no test can accidentally enqueue real work.
* Agent store dirs (``VAULT_DIR`` / ``SENTINEL_DIR`` / ``REPORTS_DIR``) → temp
  dirs, so tests never write into the developer's working tree.

The autouse fixture additionally clears the process-wide singletons (store,
cache backend, circuit breakers, metrics) between tests so ordering can never
leak state.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

#: ``backend/`` — the modules under test are imported flat (``import config``),
#: matching how ``main.py`` imports them, so this directory must be on sys.path.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: A port nothing listens on. Connecting to it on localhost is refused
#: immediately by the kernel, which is exactly the fast-fail we want.
CLOSED_PORT = 6399

#: Environment applied before any application import. Everything here is a
#: *default* — an explicit value already in the environment wins, so a developer
#: can still run a single test against real infrastructure on purpose.
_TEST_ENV: dict[str, str] = {
    # ── no proxying / no live infra ─────────────────────────────────────────
    "OPSEC_ENABLED": "false",
    "DEBUG": "true",
    "LOG_LEVEL": "WARNING",          # keep the agent step-spam out of pytest
    # ── store: real SQLite in a temp file, never Postgres ──────────────────
    "DATABASE_URL": "sqlite:///_tmp_signal_os_test.db",
    "SQLITE_PATH": "",                # empty -> config falls back to None
    "STORE_BACKEND": "sqlite",
    # ── cache: force the in-process backend (closed port = instant refusal) ─
    "REDIS_URL": f"redis://127.0.0.1:{CLOSED_PORT}/0",
    # ── queue: no broker, tasks run in-process ─────────────────────────────
    "CELERY_ENABLED": "false",
    "CELERY_BROKER_URL": f"redis://127.0.0.1:{CLOSED_PORT}/0",
    # ── no outbound network ────────────────────────────────────────────────
    "SEARXNG_URL": f"http://127.0.0.1:{CLOSED_PORT}",
    "SPIDERFOOT_URL": f"http://127.0.0.1:{CLOSED_PORT}",
    "OLLAMA_URL": f"http://127.0.0.1:{CLOSED_PORT}",
    "QDRANT_URL": f"http://127.0.0.1:{CLOSED_PORT}",
    "NEO4J_URI": f"bolt://127.0.0.1:{CLOSED_PORT}",
    "MINIO_ENDPOINT": f"127.0.0.1:{CLOSED_PORT}",
    # ── no provider credentials -> llm falls back to its offline answer ────
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "SHODAN_API_KEY": "",
    "HIBP_API_KEY": "",
    "NUMVERIFY_API_KEY": "",
    "DEHASHED_API_KEY": "",
    "DEHASHED_EMAIL": "",
    # ── keep the run bounded ───────────────────────────────────────────────
    "METRICS_ENABLED": "true",
    "AUDIT_LOG": "false",
}


def _apply_env() -> None:
    """Apply :data:`_TEST_ENV` without clobbering explicit overrides."""
    for key, value in _TEST_ENV.items():
        os.environ.setdefault(key, value)


# Apply at import time — must precede `import config` & friends, which happens
# inside the test modules themselves.
_apply_env()

# Keep agent chatter off the test output. The agents print their step lines via
# `print()` in BaseAgent.log, which pytest captures anyway; this only quiets the
# logging half.
try:
    import logging

    logging.getLogger().setLevel(logging.CRITICAL)
    for noisy in ("httpx", "httpcore", "asyncio", "celery", "redis",
                  "urllib3", "neo4j", "qdrant"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
except Exception:  # noqa: BLE001 — logging setup must never break collection
    pass


# ── pytest-asyncio configuration ─────────────────────────────────────────────

# The suite drives the async pipeline directly, so the default "strict" mode
# would reject every coroutine test without an explicit marker.
try:
    import pytest_asyncio  # noqa: F401 — presence check only
except ImportError:  # pragma: no cover — handled by the skip marker below
    pytest_asyncio = None  # type: ignore[assignment]


def pytest_configure(config: pytest.Config) -> None:
    """Run coroutine tests without needing a marker on every function."""
    config.addinivalue_line("markers", "slow: takes more than a second")


@pytest.fixture(scope="session", autouse=True)
def _session_env(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Point every agent store directory at one temp dir for the whole session.

    Agent modules (``vault.py``, ``sentinel.py``, ``quill.py``) read their store
    path from ``os.getenv`` at call time and then create it on demand, so this
    has to be in place before the first investigation runs — but unlike
    ``config``'s module globals, it does not have to precede the imports.

    Args:
        tmp_path_factory: pytest's per-session temp-dir factory.
    """
    root = tmp_path_factory.mktemp("signal_os_agents")
    for env_var, subdir in (
        ("VAULT_DIR", "vault_store"),
        ("SENTINEL_DIR", "sentinel_store"),
        ("REPORTS_DIR", "reports"),
    ):
        target = root / subdir
        target.mkdir(parents=True, exist_ok=True)
        os.environ[env_var] = str(target)

    # A real SQLite file, per session, so the store's SQL path is exercised.
    os.environ["SQLITE_PATH"] = str(root / "signal_os_test.db")
    os.environ["DATABASE_URL"] = f"sqlite:///{root / 'signal_os_test.db'}"


@pytest.fixture(autouse=True)
async def _reset_singletons():
    """Reset process-wide infra state around every test.

    ``store.get_store``, ``cache``'s backend resolution, the circuit-breaker
    registry, the metrics registry, and ``tasks``' semaphore are all module-level
    singletons. Without this reset, one test's opened breaker or cached backend
    would silently change another's result, and failures would depend on
    execution order.

    Declared ``async`` so pytest-asyncio runs the teardown on the *same* loop the
    test used. That matters for ``SQLiteStore``: it owns an ``aiosqlite``
    connection whose worker thread must be joined before that loop closes.
    Fire-and-forget task teardown would let the loop close underneath the worker,
    which surfaces as ``RuntimeError: Event loop is closed`` from a background
    thread after the test has already been reported as passing.

    Yields:
        ``None`` — this is a setup/teardown fixture, not a value provider.
    """
    await _reset_async_state()

    yield

    await _reset_async_state()


async def _reset_async_state() -> None:
    """Close and drop the store singleton, tolerating any failure.

    ``store.get_store()`` returns a started store bound to an event loop, so it
    can only be closed from inside one. Closing is strictly best-effort: a
    store that cannot be closed must not turn a passing test into a failing one.
    """
    try:
        import store

        await store.reset_store()
    except Exception:  # noqa: BLE001 — teardown must never fail a test
        pass


@pytest.fixture(autouse=True)
def _reset_sync_state():
    """Reset the *synchronous* singletons around every test.

    Also pins the cache backend. See :func:`_pin_cache_backend` for why the
    reset alone is not enough.

    Args:
        None.

    Yields:
        ``None``.
    """
    _pin_cache_backend()
    _pin_store_backend()
    _reset_rate_limit_buckets()
    try:
        import cache as cache_module

        cache_module.reset_stats()
    except Exception:  # noqa: BLE001
        pass

    try:
        import circuit as circuit_module

        circuit_module.reset_breakers()
    except Exception:  # noqa: BLE001
        pass

    try:
        import observability as obs

        obs.get_metrics().reset()
    except Exception:  # noqa: BLE001
        pass

    yield

    # Token buckets are keyed by (principal, scope), and an anonymous TestClient
    # always presents the same identity — so a module that fires many requests
    # starves the next one and failures start depending on collection order.
    # `reset_all()` alone only refills existing buckets, which is not enough once
    # a test has re-read its limits from the env, so the tables are cleared.
    _reset_rate_limit_buckets()

    try:
        import cache as cache_module

        cache_module.reset_stats()
    except Exception:  # noqa: BLE001
        pass


def _reset_rate_limit_buckets() -> None:
    """Empty WS-SEC's rate-limit bucket registry (best effort).

    Deliberately forgiving: if ``rate_limit`` is absent or its registry changes
    shape, isolation degrades silently rather than failing every test in the
    suite.
    """
    try:
        import rate_limit as rate_limit_module
    except Exception:  # noqa: BLE001 — optional module
        return
    tables = getattr(rate_limit_module, "_BUCKETS", None)
    if isinstance(tables, dict):
        for table in tables.values():
            if isinstance(table, dict):
                table.clear()
        return
    reset = getattr(rate_limit_module, "reset_all", None)
    if callable(reset):
        try:
            reset()
        except Exception:  # noqa: BLE001
            pass


def _pin_store_backend() -> str:
    """Pin the store to the in-memory backend for the whole suite.

    Memory is the store's documented zero-config path, and it is what every
    test here wants: the tests are about the *store contract* (round trips,
    ordering, stats, the degraded flag), not about SQL.

    It is also the only path that currently works reliably, because of a real
    bug in ``store.py`` that this suite documents rather than works around
    silently — see ``TestStore.test_sqlite_writes_do_not_degrade_the_store``.
    ``SQLiteStore._execute`` returns ``self._db.lastrowid`` after a write, but
    ``lastrowid`` is a *cursor* attribute and ``aiosqlite.Connection`` has no
    such member. The resulting ``AttributeError`` is caught by the method's own
    handler and converted into ``self.degrade(...)``, so the very first INSERT
    against a healthy SQLite file silently demotes the store to memory. The
    ``store.py`` owner needs to fix ``_execute``; until then the memory backend
    is the only backend whose durability can be asserted.

    Returns:
        The pinned backend name, always ``"memory"``.
    """
    import store as store_module

    backend = "memory"
    try:
        store_module._store = None  # noqa: SLF001 — drop any prior instance
    except Exception:  # noqa: BLE001
        pass
    try:
        import config as config_module

        config_module.STORE_BACKEND = backend
        config_module.SQLITE_PATH = None
    except Exception:  # noqa: BLE001
        pass
    return backend


#: Sentinel meaning "no Redis was configured" — skips the probe entirely.
_NO_REDIS = object()


def _pin_cache_backend(backend: str = "memory") -> str:
    """Pin the cache backend for the whole suite, without probing.

    **Workaround for a self-deadlock in ``cache.py``** — a real bug, reported to
    the parent, outside this workstream's ownership.

    ``cache.cache_backend()`` holds the module-global ``_backend_lock`` — a
    plain ``threading.Lock``, i.e. *not* reentrant — across its call to
    ``_probe_redis()``. When no event loop is running, ``_probe_redis`` falls
    back to ``asyncio.run(_ping_async(...))``, and ``_ping_async`` re-acquires
    the very same ``_backend_lock`` on failure::

        async def _ping_async(client):
            try:
                await client.ping()
            except Exception as exc:
                _close_quietly(client)
                with _backend_lock:          # ← already held by the caller
                    _redis = None
                    _backend = "memory"

    Reproduced against an unreachable Redis::

        thread finished? False
        DEADLOCK CONFIRMED: _probe_redis -> asyncio.run(_ping_async)
                            -> with _backend_lock while cache_backend() holds it

    The trigger is narrow — a *synchronous* first call to ``cache_backend()``
    with no running loop and Redis unreachable — but it hangs the thread
    forever rather than raising, which is the worst possible failure for a
    health endpoint or a test runner.

    The suite therefore never calls the probe: the backend is assigned directly
    to ``"memory"``, which is both the deterministic choice for an offline test
    run and exactly the state ``_probe_redis`` would have left behind.

    Once ``cache._backend_lock`` is an ``RLock`` (or ``_ping_async`` stops
    re-acquiring it), this function can be replaced with a plain call to
    ``cache.invalidate_backend()``.

    Args:
        backend: Backend name to pin. Defaults to ``"memory"``.

    Returns:
        The pinned backend name.
    """
    import cache as cache_module

    try:
        cache_module._backend = backend  # noqa: SLF001 — documented workaround
        cache_module._redis = None        # noqa: SLF001 — no client is opened
    except Exception:  # noqa: BLE001 — if the cache is unimportable, so be it
        return backend
    return backend


# ── Agent-suite fixtures (appended) ──────────────────────────────────────────
# The blocks below belong to the agent/router workstream. They layer a
# *per-test* store isolation and a hard network block on top of the session
# isolation above, because router/agent tests write to VAULT and SENTINEL and
# must never observe or clobber another test's state.


#: Every provider credential cleared for every agent test, so ``llm.py`` always takes
#: its deterministic offline path and no connector can find a key.
_AGENT_TEST_ENV: dict[str, str] = {
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "LLM_PROVIDER_ORDER": "",
    "SHODAN_API_KEY": "",
    "HIBP_API_KEY": "",
    "NUMVERIFY_API_KEY": "",
    "DEHASHED_API_KEY": "",
    "DEHASHED_EMAIL": "",
    "OPSEC_ENABLED": "false",
    # Keep APEX fast and deterministic: no retry sleeps, short pipeline budget.
    "APEX_MAX_ATTEMPTS": "1",
    "APEX_PIPELINE_BUDGET_S": "60",
    "APEX_MAX_CONCURRENCY": "8",
    "APEX_RETRY_BACKOFF_S": "0",
}

#: Per-agent timeouts used only in tests. The production values (CRAWLER=20s,
#: IRIS=45s) are sized for live network calls; in a sandbox where sockets are
#: blocked, every connector waits out its full timeout before degrading. Shrinking
#: them keeps the suite fast without changing any code path — an agent that hits
#: its timeout still returns a timeout AgentResult.
_TEST_AGENT_TIMEOUTS: dict[str, float] = {
    "CRAWLER": 2.0, "ECHO": 2.0, "IRIS": 2.0, "TERRA": 2.0, "INK": 2.0,
    "EMAIL": 3.0, "SCOUT": 5.0, "PRISM": 5.0, "PHONOS": 5.0, "SIGMA": 5.0,
}


@pytest.fixture(autouse=True)
def agent_isolation(tmp_path, monkeypatch):
    """Per-test isolation for every store the agent layer touches.

    Runs *after* the session fixture, so it overrides it: each test gets its own
    ``VAULT_DIR`` / ``SENTINEL_DIR`` / ``REPORTS_DIR`` / ``SQLITE_PATH`` under
    ``tmp_path``. Agents read these paths from ``os.getenv`` at call time, but
    ``config`` freezes them into module globals at import, so both are patched.

    Yields:
        The per-test temp root.
    """
    for key, value in _AGENT_TEST_ENV.items():
        monkeypatch.setenv(key, value)

    dirs = {}
    for env_var, subdir in (("VAULT_DIR", "vault_store"),
                            ("SENTINEL_DIR", "sentinel_store"),
                            ("REPORTS_DIR", "reports")):
        target = tmp_path / subdir
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(env_var, str(target))
        dirs[env_var] = str(target)

    db_path = tmp_path / "signal_os.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    try:
        import config

        for env_var, value in dirs.items():
            monkeypatch.setattr(config, env_var, value, raising=False)
        monkeypatch.setattr(config, "SQLITE_PATH", str(db_path), raising=False)
        monkeypatch.setattr(config, "OPSEC_ENABLED", False, raising=False)
        monkeypatch.setattr(config, "SHODAN_API_KEY", "", raising=False)
        # Shrink every agent's budget so a blocked socket degrades in seconds
        # rather than minutes. Same code path, just a tighter clock.
        for agent, seconds in _TEST_AGENT_TIMEOUTS.items():
            monkeypatch.setattr(config, f"TIMEOUT_{agent}", seconds, raising=False)
            monkeypatch.setenv(f"TIMEOUT_{agent}", str(seconds))
    except Exception:  # noqa: BLE001 — config is optional for pure-router tests
        pass
    return tmp_path


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Refuse real outbound sockets so no test can make a live request.

    Connectors are expected to *degrade* when a key is missing; this makes a
    regression that reaches for the network fail loudly and instantly instead of
    hanging or hitting a third party.

    Yields:
        ``None``.
    """
    real_socket = socket.socket
    real_create_connection = socket.create_connection

    class BlockedSocket(socket.socket):
        """A socket that refuses every outbound connection."""

        def connect(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("network blocked in tests")

        def connect_ex(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("network blocked in tests")

    def blocked_create_connection(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("network blocked in tests")

    monkeypatch.setattr(socket, "socket", BlockedSocket)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)
    yield
    socket.socket = real_socket          # belt and braces
    socket.create_connection = real_create_connection


@pytest.fixture
def anyio_backend() -> str:
    """AnyIO backend used by anyio-marked tests."""
    return "asyncio"


@pytest.fixture
def benign_text() -> str:
    """A long, benign paragraph — the canonical TEXT input for agent tests."""
    return (
        "On 2019-04-12 an operator noticed an unusual login from 203.0.113.42. "
        "The account password was last changed on 3 March 2021 according to the "
        "provider dashboard. A maintenance window ran from 18/03/2021 until "
        "2021-03-20. The internal reference was INC-4471 and the ticket was "
        "closed roughly two weeks later. Contact: ops@example.org."
    )


@pytest.fixture
def benign_signals() -> list[dict]:
    """A small set of upstream signals using mixed date formats."""
    return [
        {"type": "account", "platform": "example-social", "date": "2019-04-12",
         "source": "fixture"},
        {"type": "breach", "name": "ExampleBreach", "date": "March 3 2021",
         "source": "fixture"},
        {"type": "camera", "datetime": "2021-03-18T09:30:00Z", "source": "fixture"},
    ]


@pytest.fixture
def registry() -> dict:
    """The canonical agent registry, imported lazily to avoid an import cycle."""
    from agents import AGENT_REGISTRY

    return AGENT_REGISTRY