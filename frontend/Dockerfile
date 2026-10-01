# frontend/Dockerfile — production Next.js image for Signal-OS.
#
# Replaces the previous dev scaffold, which ran `next dev`. That is a
# development server: no production optimisation, on-demand recompilation, and
# it is explicitly not supported for production use by the Next.js team. This
# image runs a real `next build` and serves it with `next start`.
#
# Build from the repo root so the shared .dockerignore applies:
#   docker build -f frontend/Dockerfile -t signal-os/frontend ./frontend

# ── Stage 1: dependencies ───────────────────────────────────────────────────
# Separate from the source copy so `npm ci` is only re-run when the lockfile
# changes. `npm ci` (not `npm install`) is what CI and reproducibility require:
# it installs exactly the lockfile and fails if package.json and the lockfile
# disagree.
FROM node:20-alpine AS deps

WORKDIR /app
RUN apk add --no-cache libc6-compat

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# ── Stage 2: build ──────────────────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app
RUN apk add --no-cache libc6-compat

ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* values are inlined into the client bundle at BUILD time, not at
# runtime. So this value is a build arg, not a runtime env: whatever is baked
# here is what ships. Default to empty = same-origin /api, which the frontend
# proxies to the backend (see frontend/lib/api.ts). Only override it when the
# API genuinely lives on another origin.
ARG NEXT_PUBLIC_API_URL=""
ARG NEXT_PUBLIC_SIGNAL_API_KEY=""
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_SIGNAL_API_KEY=${NEXT_PUBLIC_SIGNAL_API_KEY}

# `next build` performs type-checking, so a type error fails the image build
# rather than shipping a broken bundle.
RUN npm run build

# ── Stage 3: runtime ────────────────────────────────────────────────────────
# Starts from a clean node:20-alpine without the dev node_modules and without
# the Next.js build toolchain, so the shipped image carries only production
# dependencies plus the compiled output.
FROM node:20-alpine AS runtime

LABEL org.opencontainers.image.title="Signal-OS frontend" \
      org.opencontainers.image.description="Next.js 16 dashboard for Signal-OS" \
      org.opencontainers.image.source="https://github.com/Matthew-Selvam/samaritan-os"

WORKDIR /app
RUN apk add --no-cache libc6-compat curl

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3001 \
    HOSTNAME=0.0.0.0

# next needs the root uid to drop privileges, so the runtime user is created
# with the next owner's uid and then the process switches to it.
RUN addgroup --system --gid 10001 nodejs \
    && adduser --system --uid 10001 --ingroup nodejs nextjs

# Production dependency tree only (no devDependencies: no typescript, no
# tailwind compiler, no type packages).
COPY --from=builder --chown=nextjs:nodejs /app/node_modules ./node_modules
COPY --from=builder --chown=nextjs:nodejs /app/.next ./.next
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/package.json ./package.json
COPY --from=builder --chown=nextjs:nodejs /app/next.config.ts ./next.config.ts

# server.js is Next's standalone-style server entrypoint shim; writing it
# explicitly avoids depending on a build-output detail.
RUN printf '%s\n' \
    'const { createServer } = require("http")' \
    'const next = require("next")' \
    'const app = next({ dev: false, hostname: process.env.HOSTNAME, port: process.env.PORT })' \
    'const handle = app.getRequestHandler()' \
    'app.prepare().then(() => createServer((req, res) => handle(req, res)).listen(process.env.PORT, process.env.HOSTNAME))' \
    > server.js \
    && chown nextjs:nodejs server.js

USER nextjs

EXPOSE 3001

# Hits the dashboard root. The frontend has no /api/health of its own — it
# proxies to the backend, and the backend is the service with a real health
# contract.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/" || exit 1

CMD ["node", "server.js"]
