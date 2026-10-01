# Dockerfile (repo root) — all-in-one production image for Signal-OS.
#
# Builds BOTH the Next.js frontend and the FastAPI backend into a single slim
# runtime, plus a process supervisor, so the whole platform ships as one
# artifact. This is the image the VPS / bare-metal instructions in
# docs/DEPLOY.md use, and the one Railway-style single-container deploys want.
#
# For a split deployment (separate frontend and backend containers) use
# backend/Dockerfile and frontend/Dockerfile instead.
#
# Build from the repo root:
#   docker build -t signal-os:latest .

# ── Stage 1: frontend build ─────────────────────────────────────────────────
FROM node:20-alpine AS frontend-build

WORKDIR /frontend-build
RUN apk add --no-cache libc6-compat

# NEXT_PUBLIC_* is inlined at BUILD time. Empty default = same-origin /api.
ARG NEXT_PUBLIC_API_URL=""
ARG NEXT_PUBLIC_SIGNAL_API_KEY=""
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_SIGNAL_API_KEY=${NEXT_PUBLIC_SIGNAL_API_KEY} \
    NEXT_TELEMETRY_DISABLED=1

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ── Stage 2: backend wheels ─────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS backend-build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY backend/requirements.txt ./

# Same two-step shape as backend/Dockerfile, and for the same reason: the wheel
# step must include the transitive closure, or the --no-index install below
# cannot find starlette/anyio/etc.
RUN pip install --upgrade pip setuptools wheel \
    && pip wheel --wheel-dir /wheels -r requirements.txt \
    && pip install --prefix=/install --no-index --find-links=/wheels -r requirements.txt

# ── Stage 3: runtime — python base, node + tini on top ──────────────────────
# Python is the base because the backend is the harder-to-satisfy half
# (psycopg2, cryptography); node is installed from the official binary tarball
# rather than by stacking two distro layers.
FROM python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="Signal-OS" \
      org.opencontainers.image.description="Signal-OS — AI-native multimodal intelligence fusion platform (frontend + backend)" \
      org.opencontainers.image.source="https://github.com/Matthew-Selvam/samaritan-os"

ARG NODE_VERSION=20.19.5

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH=/usr/local/node/bin:$PATH \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates libpq5 tini xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node runtime, verified against the published SHASUMS.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) node_arch=x64 ;; \
        arm64) node_arch=arm64 ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"; \
    grep " node-v${NODE_VERSION}-linux-${node_arch}.tar.xz\$" SHASUMS256.txt | sha256sum -c -; \
    tar -xJf "node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" -C /usr/local --strip-components=1; \
    rm -f "node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" SHASUMS256.txt; \
    node --version; npm --version

# The unprivileged user must exist BEFORE any `--chown=signal:signal` COPY,
# otherwise those copies fail to resolve the name.
RUN groupadd --system --gid 10001 signal \
    && useradd --system --uid 10001 --gid signal --home-dir /app --shell /usr/sbin/nologin signal

# Python deps.
COPY --from=backend-build /install /usr/local

# Backend source.
WORKDIR /app
COPY --chown=signal:signal backend/ /app/

# Frontend build output + production node_modules, under /app/frontend.
RUN mkdir -p /app/frontend
COPY --from=frontend-build --chown=signal:signal /frontend-build/node_modules /app/frontend/node_modules
COPY --from=frontend-build --chown=signal:signal /frontend-build/.next      /app/frontend/.next
COPY --from=frontend-build --chown=signal:signal /frontend-build/public     /app/frontend/public
COPY --from=frontend-build --chown=signal:signal /frontend-build/package.json /app/frontend/package.json
COPY --from=frontend-build --chown=signal:signal /frontend-build/next.config.ts /app/frontend/next.config.ts

# The Next.js server shim, written here rather than copied so the all-in-one
# image needs no extra build-stage file. Kept byte-identical in intent to the
# one generated inside frontend/Dockerfile's runtime stage.
RUN printf '%s\n' \
    'const { createServer } = require("http")' \
    'const next = require("next")' \
    'const app = next({ dev: false, hostname: process.env.HOSTNAME, port: process.env.PORT })' \
    'const handle = app.getRequestHandler()' \
    'app.prepare().then(() => createServer((req, res) => handle(req, res)).listen(process.env.PORT, process.env.HOSTNAME))' \
    > /app/frontend/server.js

# supervisor runs the two processes; it is the Debian package, not the
# pip-installed one, so the stock config paths apply.
RUN apt-get update && apt-get install -y --no-install-recommends supervisor \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=signal:signal infra/supervisord.conf /etc/supervisor/conf.d/signal-os.conf

# The agent memory stores must be writable by the runtime user. Compose mounts
# named volumes here; creating them in the image also makes a bare
# `docker run` of this image work.
RUN mkdir -p /app/vault_store /app/sentinel_store /app/reports \
    && chown -R signal:signal /app

USER signal

# Backend 8766, frontend 3001.
EXPOSE 8766 3001

ENV HOST=0.0.0.0 \
    PORT=8766 \
    WORKERS=2 \
    FRONTEND_PORT=3001 \
    DEBUG=false \
    ENVIRONMENT=production \
    LOG_LEVEL=INFO \
    OPSEC_ENABLED=false

# tini is PID 1: it reaps the zombies that a two-process image inevitably
# produces, and forwards SIGTERM so `docker stop` actually shuts down cleanly
# instead of waiting out the 10s kill timeout.
ENTRYPOINT ["/usr/bin/tini", "--"]

# The backend is the health contract; the frontend is checked with a second
# probe on its own port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" \
        && curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/" || exit 1

CMD ["supervisord", "-c", "/etc/supervisor/supervisord.conf"]
