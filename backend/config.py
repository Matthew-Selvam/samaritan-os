"""
config.py — Centralized Configuration
======================================
All environment variables, constants, and feature flags in one place.
Makes the system highly modular and testable.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Environment ───────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
DEBUG = os.getenv("DEBUG", "true").lower() == "true"
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# ── API & Network ──────────────────────────────────────────────────────────────

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8766"))
WORKERS = int(os.getenv("WORKERS", "4"))

# ── Database & Storage ────────────────────────────────────────────────────────

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://signal:signal@localhost:5432/signal_os"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")

# ── Agent-specific stores ──────────────────────────────────────────────────────
# On Vercel, only /tmp is writable, and it's wiped between cold starts — VAULT
# and SENTINEL still work per-invocation but recurrence won't persist across
# them. A long-lived host (Railway/Render/a VPS) gets real persistence.

_ON_VERCEL = bool(os.getenv("VERCEL"))
_default_store_root = "/tmp/signal-os" if _ON_VERCEL else str(BASE_DIR / "backend")

VAULT_DIR = os.getenv("VAULT_DIR", f"{_default_store_root}/vault_store")
SENTINEL_DIR = os.getenv("SENTINEL_DIR", f"{_default_store_root}/sentinel_store")
REPORTS_DIR = os.getenv("REPORTS_DIR", f"{_default_store_root}/reports")

# ── Security & Auth ────────────────────────────────────────────────────────────

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")

# ── OSINT Connectors: API Keys (Optional) ──────────────────────────────────────

SHODAN_API_KEY = os.getenv("SHODAN_API_KEY", "")
HIBP_API_KEY = os.getenv("HIBP_API_KEY", "")
NUMVERIFY_API_KEY = os.getenv("NUMVERIFY_API_KEY", "")
DEHASHED_EMAIL = os.getenv("DEHASHED_EMAIL", "")
DEHASHED_API_KEY = os.getenv("DEHASHED_API_KEY", "")

# ── OPSEC & Tor ────────────────────────────────────────────────────────────────

OPSEC_ENABLED = os.getenv("OPSEC_ENABLED", "true").lower() == "true"
TOR_PROXY = os.getenv("TOR_PROXY", "socks5h://127.0.0.1:9050")
TOR_CONTROL = os.getenv("TOR_CONTROL", "127.0.0.1:9051")
TOR_CONTROL_PASSWORD = os.getenv("TOR_CONTROL_PASSWORD", "")

# ── Audio/ML Models ────────────────────────────────────────────────────────────

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")

# ── Pipeline Configuration ────────────────────────────────────────────────────

# Correlation tier: agents that run *after* the primary swarm
CORRELATION_TIER = ["NEXUS", "KRONOS", "VAULT", "SENTINEL", "QUILL"]

# Primary agents that run *during* the concurrent swarm
PRIMARY_AGENTS = ["SCOUT", "PHONOS", "EMAIL", "CRAWLER", "IRIS", "ECHO",
                  "PRISM", "TERRA", "INK", "SIGMA"]

# ── Timeouts (seconds) ─────────────────────────────────────────────────────────

TIMEOUT_PHONOS = float(os.getenv("TIMEOUT_PHONOS", "40"))
TIMEOUT_EMAIL = float(os.getenv("TIMEOUT_EMAIL", "30"))
TIMEOUT_CRAWLER = float(os.getenv("TIMEOUT_CRAWLER", "20"))
TIMEOUT_IRIS = float(os.getenv("TIMEOUT_IRIS", "45"))
TIMEOUT_ECHO = float(os.getenv("TIMEOUT_ECHO", "30"))
TIMEOUT_TERRA = float(os.getenv("TIMEOUT_TERRA", "10"))
TIMEOUT_SCOUT = float(os.getenv("TIMEOUT_SCOUT", "60"))
TIMEOUT_PRISM = float(os.getenv("TIMEOUT_PRISM", "60"))
TIMEOUT_BREACH = float(os.getenv("TIMEOUT_BREACH", "15"))
# Extended per-agent budgets. Every registry agent has an explicit entry so
# `get_timeout()` never falls through to the generic default for a real agent.
TIMEOUT_APEX = float(os.getenv("TIMEOUT_APEX", "180"))
TIMEOUT_SIGMA = float(os.getenv("TIMEOUT_SIGMA", "45"))
TIMEOUT_INK = float(os.getenv("TIMEOUT_INK", "45"))
TIMEOUT_NEXUS = float(os.getenv("TIMEOUT_NEXUS", "45"))
TIMEOUT_KRONOS = float(os.getenv("TIMEOUT_KRONOS", "40"))
TIMEOUT_VAULT = float(os.getenv("TIMEOUT_VAULT", "40"))
TIMEOUT_SENTINEL = float(os.getenv("TIMEOUT_SENTINEL", "40"))
TIMEOUT_QUILL = float(os.getenv("TIMEOUT_QUILL", "60"))

# ── Features (Feature Flags) ───────────────────────────────────────────────────

FEATURE_PDF_EXPORT = os.getenv("FEATURE_PDF_EXPORT", "true").lower() == "true"
FEATURE_EMAIL_ENUM = os.getenv("FEATURE_EMAIL_ENUM", "true").lower() == "true"
FEATURE_DARK_WEB_ALERTS = os.getenv("FEATURE_DARK_WEB_ALERTS", "false").lower() == "true"

# ── Logging ────────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG" if DEBUG else "INFO")
LOG_FORMAT = os.getenv(
    "LOG_FORMAT",
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)


#: Fallback used for any agent without an explicit entry below.
DEFAULT_TIMEOUT = float(os.getenv("TIMEOUT_DEFAULT", "30"))


def get_timeout(agent_name: str) -> float:
    """Get the configured timeout for an agent.

    Covers **every** agent in the registry (APEX plus all 14 specialists).
    Lookup is case- and whitespace-insensitive; anything unrecognised falls
    back to :data:`DEFAULT_TIMEOUT` rather than raising, so a newly added
    agent degrades to a sane default instead of crashing the caller.

    Args:
        agent_name: Registry name, e.g. ``"KRONOS"``.

    Returns:
        Timeout in seconds.
    """
    timeouts = {
        "APEX": TIMEOUT_APEX,
        "SCOUT": TIMEOUT_SCOUT,
        "SIGMA": TIMEOUT_SIGMA,
        "PHONOS": TIMEOUT_PHONOS,
        "EMAIL": TIMEOUT_EMAIL,
        "CRAWLER": TIMEOUT_CRAWLER,
        "IRIS": TIMEOUT_IRIS,
        "ECHO": TIMEOUT_ECHO,
        "PRISM": TIMEOUT_PRISM,
        "TERRA": TIMEOUT_TERRA,
        "INK": TIMEOUT_INK,
        "NEXUS": TIMEOUT_NEXUS,
        "KRONOS": TIMEOUT_KRONOS,
        "VAULT": TIMEOUT_VAULT,
        "SENTINEL": TIMEOUT_SENTINEL,
        "QUILL": TIMEOUT_QUILL,
        # Historical alias kept so existing callers keep working.
        "BREACH": TIMEOUT_BREACH,
    }
    try:
        key = str(agent_name or "").strip().upper()
    except Exception:  # noqa: BLE001 — a weird object must not raise here
        return DEFAULT_TIMEOUT
    return timeouts.get(key, DEFAULT_TIMEOUT)


def is_feature_enabled(feature_name: str) -> bool:
    """Check if a feature is enabled."""
    features = {
        "pdf_export": FEATURE_PDF_EXPORT,
        "email_enum": FEATURE_EMAIL_ENUM,
        "dark_web": FEATURE_DARK_WEB_ALERTS,
    }
    return features.get(feature_name, False)


# ── Hardening: cache, store, circuit, queue, LLM ────────────────────────────
# Appended by the infrastructure workstream. Every value below is read from the
# environment with a safe default so the platform boots with zero configuration;
# the infra modules all fall back to in-process behaviour when a backing
# service is missing. Nothing here may raise at import time.

# ── Cache (backend/cache.py) ─────────────────────────────────────────────────

#: Default TTL applied by :func:`cache.cache_set` when the caller passes no ttl.
CACHE_TTL_SECONDS = float(os.getenv("CACHE_TTL_SECONDS", "300"))
#: Maximum live entries in the in-process TTL-LRU used when Redis is absent.
CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", "1000"))
#: When true a Redis outage is logged at ERROR and never silently demoted.
REDIS_REQUIRED = os.getenv("REDIS_REQUIRED", "false").lower() == "true"

# ── Store (backend/store.py) ─────────────────────────────────────────────────

#: ``auto`` | ``memory`` | ``sqlite`` | ``postgres``. ``auto`` derives the
#: backend from ``DATABASE_URL`` and falls back to SQLite, then memory.
STORE_BACKEND = os.getenv("STORE_BACKEND", "auto").strip().lower() or "auto"
#: SQLite database file. ``None`` keeps the store in memory, which is what makes
#: the zero-config deployment possible.
SQLITE_PATH = os.getenv("SQLITE_PATH") or None
#: Row-retention cap for the in-memory tables of every store backend.
STORE_MAX_ROWS = int(os.getenv("STORE_MAX_ROWS", "5000"))

# ── Circuit breaker (backend/circuit.py) ─────────────────────────────────────

#: Consecutive failures that trip a breaker open.
CIRCUIT_FAIL_THRESHOLD = int(os.getenv("CIRCUIT_FAIL_THRESHOLD", "5"))
#: Seconds a tripped breaker stays open before admitting a half-open probe.
CIRCUIT_RESET_AFTER = float(os.getenv("CIRCUIT_RESET_AFTER", "30"))

# ── Queue / Celery (backend/celery_app.py, backend/tasks.py) ─────────────────

#: Upper bound on investigations running concurrently in one process. The local
#: (no-Celery) path uses it as a semaphore so a burst of submissions cannot
#: starve the event loop.
MAX_CONCURRENT_INVESTIGATIONS = int(os.getenv("MAX_CONCURRENT_INVESTIGATIONS", "4"))
#: Broker/backend DSN for Celery. Defaults to ``REDIS_URL``; an empty value
#: disables Celery entirely and forces the local path.
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL") or REDIS_URL
#: Master switch. When false (or when Celery/broker is unavailable) every task
#: runs in-process via :func:`tasks.run_investigation_local`.
CELERY_ENABLED = os.getenv("CELERY_ENABLED", "true").lower() == "true"

# ── HTTP / upstream request budget ───────────────────────────────────────────

#: Default ceiling for a single outbound HTTP request (connectors, LLM, API).
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))

# ── LLM (mirrors the env contract documented in backend/llm.py) ─────────────

#: When false, agents skip model calls entirely and fall back to their
#: deterministic offline answers.
LLM_ENABLED = os.getenv("LLM_ENABLED", "true").lower() == "true"
#: Per-request model timeout, matching ``LLM_DEFAULT_TIMEOUT`` in llm.py.
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT") or os.getenv("LLM_DEFAULT_TIMEOUT", "45"))
#: Default completion budget; agents may lower this to their ``token_budget``.
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))
#: Default sampling temperature. Structured extraction pins 0.0 at the call site.
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
#: Local Ollama endpoint — last provider in the chain before the offline fallback.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
#: Comma-separated provider chain; first healthy provider wins.
LLM_PROVIDER_ORDER = os.getenv(
    "LLM_PROVIDER_ORDER", "openai,anthropic,deepseek,openrouter,ollama"
)

# ── Observability toggles ────────────────────────────────────────────────────

#: Emit structured audit events for privileged actions (see observability).
AUDIT_LOG = os.getenv("AUDIT_LOG", "true").lower() == "true"
#: Collect in-process counters/timings/gauges. Off drops every recording call.
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "true").lower() == "true"
