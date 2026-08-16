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

VAULT_DIR = os.getenv("VAULT_DIR", str(BASE_DIR / "backend" / "vault_store"))
SENTINEL_DIR = os.getenv("SENTINEL_DIR", str(BASE_DIR / "backend" / "sentinel_store"))
REPORTS_DIR = os.getenv("REPORTS_DIR", str(BASE_DIR / "backend" / "reports"))

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


def get_timeout(agent_name: str) -> float:
    """Get the configured timeout for an agent."""
    timeouts = {
        "PHONOS": TIMEOUT_PHONOS,
        "EMAIL": TIMEOUT_EMAIL,
        "CRAWLER": TIMEOUT_CRAWLER,
        "IRIS": TIMEOUT_IRIS,
        "ECHO": TIMEOUT_ECHO,
        "TERRA": TIMEOUT_TERRA,
        "SCOUT": TIMEOUT_SCOUT,
        "PRISM": TIMEOUT_PRISM,
    }
    return timeouts.get(agent_name, 30.0)


def is_feature_enabled(feature_name: str) -> bool:
    """Check if a feature is enabled."""
    features = {
        "pdf_export": FEATURE_PDF_EXPORT,
        "email_enum": FEATURE_EMAIL_ENUM,
        "dark_web": FEATURE_DARK_WEB_ALERTS,
    }
    return features.get(feature_name, False)
