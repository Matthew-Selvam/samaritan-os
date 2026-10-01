# backend/Dockerfile — production FastAPI image for Signal-OS.
#
# Replaces the previous dev scaffold, which ran `uvicorn --reload` in a
# compiler-equipped image. This is a production image: no compiler, no dev
# headers, no reload, non-root, with a real HEALTHCHECK.
#
# Build from the repo root so the shared .dockerignore applies:
#   docker build -f backend/Dockerfile -t signal-os/backend ./backend

# ── Stage 1: build wheels ────────────────────────────────────────────────────
# Compiling here and installing only the resulting wheels in the runtime stage
# means the runtime image never carries gcc/libpq-dev.
FROM python:3.11-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential + libpq-dev are needed only to build source-only
# dependencies. libpq5 is a *runtime* lib and is installed in the final stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Own layer, so the slow dependency install is only re-run when
# requirements.txt changes — not on every source edit.
COPY requirements.txt ./

# Two steps, and the second one must NOT pass --no-deps:
#   1. `pip wheel` (with deps) resolves and builds every requirement *and* its
#      transitive closure into /wheels.
#   2. `pip install --no-index --find-links=/wheels` installs from that
#      directory alone, so the runtime layer has no network access and no
#      build toolchain.
# Using --no-deps in step 1 (a tempting "just my requirements" reading) silently
# omits transitive deps such as starlette/anyio, and step 2 then fails with
# "No matching distribution found".
RUN pip install --upgrade pip setuptools wheel \
    && pip wheel --wheel-dir /wheels -r requirements.txt \
    && pip install --prefix=/install --no-index --find-links=/wheels -r requirements.txt

# ── Stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="Signal-OS backend" \
      org.opencontainers.image.description="FastAPI backend + Celery worker for Signal-OS" \
      org.opencontainers.image.source="https://github.com/Matthew-Selvam/samaritan-os"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# curl is the HEALTHCHECK client; libpq5 is psycopg2's runtime dependency;
# ca-certificates is needed for outbound TLS (every connector fetches https).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libpq5 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user, fixed uid/gid so bind-mount ownership is
# predictable across hosts.
RUN groupadd --system --gid 10001 signal \
    && useradd --system --uid 10001 --gid signal --home-dir /app --shell /usr/sbin/nologin signal

# Wheels from stage 1, installed into the system prefix.
COPY --from=builder /install /usr/local

WORKDIR /app

# Source. The root .dockerignore strips .venv, __pycache__, tests and the agent
# stores from the build context, so this copies only the real modules.
COPY --chown=signal:signal . .

# The agent memory stores (VAULT/SENTINEL/REPORTS) must be writable by the
# runtime user. Compose mounts named volumes here, but creating them in the
# image means it also works standalone (`docker run` on a VPS).
RUN mkdir -p /app/vault_store /app/sentinel_store /app/reports \
    && chown -R signal:signal /app

# Never run as root.
USER signal

EXPOSE 8766

# Sanity defaults, so a bare `docker run` of this image is safe rather than
# defaulting to DEBUG=true. Compose and the deploy targets override these.
ENV HOST=0.0.0.0 \
    PORT=8766 \
    WORKERS=2 \
    DEBUG=false \
    ENVIRONMENT=production \
    LOG_LEVEL=INFO \
    OPSEC_ENABLED=false

# /api/health is a pure in-process endpoint: it does not touch Postgres, Redis
# or Neo4j, which is what makes it a valid liveness probe (a probe that
# depended on the DB would restart the app whenever the DB blipped).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# One worker by default. Uvicorn's workers=N forks processes that each hold a
# full agent registry; for a CPU-bound investigation pipeline, scaling out with
# more containers is the right lever, not more in-process workers.
CMD ["sh", "-c", "exec uvicorn main:app --host ${HOST} --port ${PORT} --workers ${WORKERS} --proxy-headers --forwarded-allow-ips='*'"]
