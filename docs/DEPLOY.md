# Deploying Signal-OS

Five targets, described honestly. The platform runs with **zero required
infrastructure** — that is a design property, not a convenience — so the
question each deployment answers is different: how much of the optional tier do
you want, and how much latency can you afford?

| Target | Fit | Read this section |
|---|---|---|
| Local dev | Full fidelity, nothing to install | [Local](#1-local-development) |
| Docker Compose | Everything, one command | [Compose](#2-docker-compose) |
| Bare VPS | Best for Tor + long investigations | [VPS](#4-bare-vps) |
| Railway | Good for the API + workers | [Railway](#3-railway) |
| Vercel | **Frontend and sync path only** | [Vercel](#5-vercel) |

---

## The one constraint that decides everything

**An investigation is a multi-second, stateful, multi-request unit of work.**

It fans out to ~10 agents and dozens of outbound HTTP calls. It cannot be split
across instances, and it cannot be resumed by a different instance. This rules
out naive serverless deployment of the main pipeline and dictates:

- **Async path** (`POST /api/investigate` → id → poll/WS) needs a process that
  can hold the work: a container, a VM, or a Celery worker.
- **Sync path** (`POST /api/investigate-sync`) does the whole thing inside one
  request and therefore *does* fit serverless — at the cost of a long response
  and no streaming.
- **WebSocket streaming** needs a long-lived process. Serverless functions
  terminate the connection at the execution cap.

---

## 1. Local development

Nothing to install beyond Python and Node. Postgres, Redis, Qdrant, Neo4j,
MinIO, SearXNG, SpiderFoot, Tor and the LLM are all optional.

```bash
git clone <repo> && cd samaritan-os
./scripts/dev-setup.sh          # venv, deps, hooks, .env from .env.example
make dev                        # backend :8766 + frontend :3001
```

Verify:

```bash
./scripts/smoke.sh              # 49 assertions, exits 0, tears down cleanly
```

With no infrastructure the store degrades to memory and the cache to an
in-process LRU. `/api/health` reports `"status": "degraded"` — **that is the
expected result here, not a fault.** See
[`RUNBOOK.md` — Degraded store](RUNBOOK.md#the-store-reports-degraded-mode).

### Useful targets

```bash
make test                       # full backend suite
make test-smoke                 # smoke against a running instance
make lint lint-fix fmt          # ruff (config in infra/ruff.toml)
cd backend && .venv/bin/python -m pytest tests/test_smoke.py -q
```

### To run the optional tier locally

```bash
docker compose up -d postgres redis qdrant neo4j minio searxng
```

Then set the matching env vars (see [Environment variables](#environment-variables))
and restart. The store will promote itself from memory to Postgres
automatically.

---

## 2. Docker Compose

The full stack: data tier, app tier, workers, OSINT tier.

```bash
cp .env.example .env             # then edit it
docker compose up -d
docker compose ps
docker compose logs -f backend
```

Services defined in `docker-compose.yml`:

| Service | Purpose |
|---|---|
| `backend` | FastAPI on `:8766`, plus Celery worker via supervisord |
| `frontend` | Next.js |
| `celery-worker` | Background investigations, `concurrency=4` |
| `celery-beat` | Scheduled tasks |
| `postgres`, `redis`, `neo4j`, `qdrant`, `minio` | Data tier (all optional) |
| `searxng` | Metasearch for SCOUT |
| `tor` | SOCKS proxy + control port for OPSEC |
| `pgweb` | Postgres admin UI |

### Tor under Compose

`ALLOW_PRIVATE_NETWORK=true` is required when SearXNG and SpiderFoot run as
private service names on the Compose network — the SSRF guard blocks private
ranges by default.

> **This is the one place that flag is safe.** It disables the private-range
> checks *and* the DNS resolution they depend on. On a host that can reach cloud
> metadata (any normal cloud VM), enabling it exposes `169.254.169.254` to SSRF.
> Never set it outside a Compose network you control.

### Health checks

The Dockerfile healthcheck polls `/api/health`; `railway.json` sets
`healthcheckTimeout` to **120 s**. The app probes every backing service on
startup, so first boot legitimately takes a while when services are slow to
come up. A 30 s timeout will restart a healthy container in a loop.

### Scaling out

```bash
docker compose up -d --scale celery-worker=4
docker compose up -d --scale backend=3
```

Two caveats, both from
[`ARCHITECTURE.md` §8](ARCHITECTURE.md#8-deployment-topology):

- **Rate-limit buckets are per-process.** Four workers means four times the
  configured `investigate` ceiling. Move the limiter to Redis-backed counting
  before you rely on it as a blast-radius control.
- **SQLite is not a multi-writer store.** With more than one app replica, set
  `STORE_BACKEND=postgres`.

---

## 3. Railway

`railway.json` is already committed: Dockerfile build, `/api/health` check,
`ON_FAILURE` restart with 5 retries.

```bash
railway init
railway up
railway variables set \
  SIGNAL_API_KEY="$(openssl rand -hex 32)" \
  AUTH_DISABLED=false DEBUG=false \
  STORE_BACKEND=sqlite SQLITE_PATH=/data/signal_os.db
```

### What the committed config gives you

```json
"healthcheckPath": "/api/health",
"healthcheckTimeout": 120,
"variables": {
  "DEBUG": "false", "OPSEC_ENABLED": "false", "AUTH_DISABLED": "false",
  "STORE_BACKEND": "sqlite", "SQLITE_PATH": "/data/signal_os.db",
  "LLM_ENABLED": "true", "LLM_ALLOW_LOCAL": "false",
  "MAX_CONCURRENT_INVESTIGATIONS": "8", "ALLOW_PRIVATE_NETWORK": "false"
}
```

Read those as deliberate choices, and note what they give up:

- **`OPSEC_ENABLED=false`** — Tor is not available on Railway, so requests go
  out from the Railway egress IP. For investigations on real people that IP is
  now part of the threat model. See
  [`SECURITY.md`](SECURITY.md#6-the-opsec--tor-layer).
- **`STORE_BACKEND=sqlite`** on `/data` — works on a single replica with a
  mounted volume, and **loses all data if the volume is not attached**. Attach
  a volume at `/data` or use `postgres`.
- **`ALLOW_PRIVATE_NETWORK=false`** — correct for a public PaaS host.

### Adding the data tier

Railway provisions Postgres and Redis as plugins; set `DATABASE_URL`,
`REDIS_URL` and `STORE_BACKEND=postgres`. Add a second service for the Celery
worker from the same Dockerfile, overriding the start command to
`celery -A celery_app.celery_app worker --loglevel=info`.

---

## 4. Bare VPS

The best target when you need Tor, long-running investigations and a stable
egress IP. This is what the platform is actually shaped for.

### Install

```bash
# 1. Tor
sudo apt install -y tor
sudo systemctl enable --now tor

# 2. The app
sudo mkdir -p /opt/signal-os && sudo chown $USER /opt/signal-os
git clone <repo> /opt/signal-os/samaritan-os
cd /opt/signal-os/samaritan-os
./scripts/dev-setup.sh
cp .env.example .env
```

### Tor configuration

Enable the control port so `POST /api/opsec/newcircuit` works:

```
# /etc/tor/torrc
SocksPort 0.0.0.0:9050
ControlPort 127.0.0.1:9051
HashedControlPassword <generated with: tor --hash-password>
CookieAuthentication 0
```

```bash
sudo systemctl restart tor
curl -s http://127.0.0.1:9050  # or check via the app:
curl -s localhost:8766/api/opsec/status | jq
```

Keep `ControlPort` on loopback only. It can rotate your egress identity; anyone
who can reach it can make your host's traffic hard to attribute.

### systemd

```ini
# /etc/systemd/system/signal-os.service
[Unit]
Description=Signal-OS backend
After=network-online.target tor.service
Wants=network-online.target

[Service]
Type=simple
User=signal
WorkingDirectory=/opt/signal-os/samaritan-os/backend
EnvironmentFile=/opt/signal-os/samaritan-os/.env
ExecStart=/opt/signal-os/samaritan-os/backend/.venv/bin/python -m uvicorn main:app \
          --host 127.0.0.1 --port 8766 --workers 4
Restart=always
RestartSec=5
# Hardening — the process only needs to read its own tree and write store dirs.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/signal-os/samaritan-os/backend/vault_store \
               /opt/signal-os/samaritan-os/backend/sentinel_store \
               /opt/signal-os/samaritan-os/backend/reports

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now signal-os
```

### TLS

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

```nginx
server {
    listen 443 ssl http2;
    server_name signal-os.example.com;

    ssl_certificate     /etc/letsencrypt/live/signal-os.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/signal-os.example.com/privkey.pem;

    # Long investigations must not be cut off mid-pipeline.
    proxy_read_timeout 300s;

    # WebSocket upgrade, required for /ws/pipeline/{id}.
    location /ws/ {
        proxy_pass http://127.0.0.1:8766;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }

    location / {
        proxy_pass http://127.0.0.1:8766;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name signal-os.example.com;
    return 301 https://$host$request_uri;
}
```

Then set `CORS_ORIGINS=https://signal-os.example.com` and
`WS_ALLOWED_ORIGINS=https://signal-os.example.com`.

> **The X-Forwarded-For header is load-bearing.** Client IPs are derived from
> it for rate-limit bucketing, auth-failure throttling and the audit trail. If
> you terminate TLS somewhere without forwarding it, every client shares one
> bucket and one audit identity.

### Sizing

One investigation is ~10 agents and dozens of outbound calls. A 2 vCPU / 4 GB
VPS handles the API plus a Celery worker with `concurrency=4` comfortably;
`LLM_ENABLED=false` keeps it light. Add memory before adding replicas if the LLM
tier is on.

---

## 5. Vercel

**Read this before deploying to Vercel.** The committed `vercel.json` points
Vercel at the **frontend**, with `api/index.py` as a Python serverless entry
that imports `backend/main.py`'s `app`.

```json
"functions": { "api/index.py": { "maxDuration": 60 } },
"rewrites": [
  { "source": "/api/(.*)", "destination": "/api/index" },
  { "source": "/ws/:path*",  "destination": "/api/index" }
]
```

### What works, and what does not

| Capability | Vercel | Why |
|---|---|---|
| Next.js frontend | ✅ | This is the build target |
| `GET` reads, health, cases CRUD | ✅ | Sub-second |
| `POST /api/investigate-sync` | ⚠️ **fragile** | Measured 2.4–3.9 s with *nothing* configured. With LLM calls and live connectors this approaches the 60 s cap. A cold start plus a real connector sweep can exceed it. |
| `POST /api/investigate` (async) | ❌ **broken by design** | The background task cannot outlive the response, and a later `GET /api/investigate/{id}` may land on a different instance with no shared store. |
| `WS /ws/pipeline/{id}` | ❌ | The function terminates at the execution cap. |
| Durable storage | ❌ | `vercel.json` pins `VAULT_DIR`/`SENTINEL_DIR` to `/tmp`, which is per-instance and ephemeral. Investigations are lost. |

`/tmp` ephemerality is the decisive one. A platform whose entire value is the
accumulated case history cannot run on per-instance ephemeral disk.

### The supported split

```
Vercel      →  frontend  +  (optional) short read-only API
VPS/Railway →  backend, Celery worker, Postgres, Tor
```

Point the frontend at the real backend with
`NEXT_PUBLIC_API_URL=https://api.example.com`.

### If you must run the API on Vercel

Use Railway or a VPS for it. Do not spend effort trying to make the async path
work: the failure mode is not an error you can handle, it is an investigation
that silently returns `queued` forever.

---

## Environment variables

From `.env.example`, plus the ones the code reads that the example omits.

### Core

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `development` | `development` also disables auth |
| `DEBUG` | `true` | `true` exposes `/api/docs` |
| `HOST` / `PORT` | `0.0.0.0` / `8766` | bind address |
| `WORKERS` | `4` | uvicorn workers; multiplies the rate-limit ceiling |
| `SECRET_KEY` | `change-me-in-production` | **change this** |
| `LOG_LEVEL` | `DEBUG` if `DEBUG` else `INFO` | |

### Auth

| Variable | Default | Notes |
|---|---|---|
| `SIGNAL_API_KEY` | — | single key, scopes `*` |
| `SIGNAL_API_KEYS` | — | `label:key:scope1,scope2`, comma-separated |
| `AUTH_DISABLED` | `false` | **never** in production |

### Network / SSRF

| Variable | Default | Notes |
|---|---|---|
| `ALLOW_PRIVATE_NETWORK` | `false` | `true` only inside a Compose network |
| `CORS_ORIGINS` | `""` | comma-separated allowlist |
| `WS_ALLOWED_ORIGINS` | `""` | WebSocket origin allowlist |

### Rate limiting

| Variable | Default | Notes |
|---|---|---|
| `RATE_LIMIT_RPM` | per-scope table | global override |
| `RATE_LIMIT_SCOPE_RPM.<scope>` | `investigate`=6, `search`=30, `read`=240, `opsec`=12 | per-scope override. **The name contains a dot** — set it with `env 'NAME=v'`, not `export`. |
| `RATE_LIMIT_BURST.<scope>` | `investigate`=2, … | bucket depth |

### Storage / cache

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | Postgres DSN |
| `STORE_BACKEND` | `auto` | `auto` \| `postgres` \| `sqlite` \| `memory` |
| `SQLITE_PATH` | — | file path |
| `STORE_MAX_ROWS` | `5000` | **retention cap per collection** |
| `REDIS_URL` | `redis://localhost:6379` | |
| `REDIS_REQUIRED` | `false` | `true` makes Redis mandatory |
| `CACHE_TTL_SECONDS` / `CACHE_MAX_SIZE` | `300` / `1000` | in-process fallback |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | localhost | |
| `QDRANT_URL` / `QDRANT_API_KEY` / `QDRANT_COLLECTION` | localhost / — / `signal_os_memory` | |
| `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | localhost / minioadmin | |
| `VAULT_DIR` / `SENTINEL_DIR` / `REPORTS_DIR` | under `backend/` | agent stores |

### AI tier

| Variable | Default | Notes |
|---|---|---|
| `LLM_ENABLED` | `true` | master kill-switch; `false` = deterministic only |
| `LLM_ALLOW_LOCAL` | `false` | **must** be true for Ollama |
| `LLM_PROVIDER_ORDER` | `openai,anthropic,deepseek,openrouter` | |
| `OLLAMA_URL` / `OLLAMA_MODEL` | localhost:11434 / — | |
| `LLM_TIMEOUT` / `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` | `45` / `2048` / `0.2` | |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY` | — | auto-detected from presence |

### OPSEC

| Variable | Default | Notes |
|---|---|---|
| `OPSEC_ENABLED` | `true` | Tor routing |
| `TOR_PROXY` | `socks5h://127.0.0.1:9050` | `socks5h` resolves via Tor |
| `TOR_CONTROL` | `127.0.0.1:9051` | for `SIGNAL NEWNYM` |
| `TOR_CONTROL_PASSWORD` | — | |

### Connectors

| Variable | Default | Notes |
|---|---|---|
| `SHODAN_API_KEY` | — | SIGMA |
| `HIBP_API_KEY`, `DEHASHED_EMAIL`, `DEHASHED_API_KEY` | — | PRISM breach check |
| `NUMVERIFY_API_KEY` | — | PHONOS |
| `SEARXNG_URL` | `https://searx.be` | SCOUT, reverse image |
| `SPIDERFOOT_URL` | `http://localhost:5001` | SCOUT |
| `PLANT_ID_API_KEY` / `PLANTNET_API_KEY` | — | TERRA |
| `WHISPER_MODEL` / `WHISPER_DEVICE` / `WHISPER_API_KEY` | `base` / `cpu` / — | ECHO |
| `BIOCLIP_ENABLED` / `BIOCLIP_OLLAMA_ENABLED` | `true` | TERRA |
| `FASTTEXT_LID_MODEL` | `models/lid176.ftz` | language detection |

Check what a deployment actually has wired:

```bash
curl -s localhost:8766/api/config | jq .env_presence
```

### Feature flags & worker

| Variable | Default | Notes |
|---|---|---|
| `FEATURE_PDF_EXPORT` | `true` | `false` makes `/report?format=pdf` 501 |
| `FEATURE_EMAIL_ENUM` | `true` | |
| `FEATURE_DARK_WEB_ALERTS` | `false` | SENTINEL dark-web monitoring |
| `DARKWEB_INDEX_URLS` / `DARKWEB_ALLOWED_HOSTS` | — | when enabled |
| `CELERY_ENABLED` | `true` | `false` runs tasks in-process |
| `CELERY_BROKER_URL` | `REDIS_URL` | |
| `MAX_CONCURRENT_INVESTIGATIONS` | `4` | semaphore ceiling |
| `METRICS_ENABLED` / `AUDIT_LOG` | `true` | |
| `CIRCUIT_FAIL_THRESHOLD` / `CIRCUIT_RESET_AFTER` | `5` / `30` | breaker tuning |

---

## Pre-deploy checklist

```bash
# 1. Secrets are real
grep -q 'change-me-in-production' .env && echo "SECRET_KEY still default" || echo "ok"

# 2. Not running in dev mode
grep -E '^(DEBUG|ENVIRONMENT|AUTH_DISABLED)=' .env
#   want DEBUG=false, ENVIRONMENT=production, AUTH_DISABLED=false

# 3. SSRF guard on
grep '^ALLOW_PRIVATE_NETWORK=' .env   # want false outside Compose

# 4. A key exists (auth fails closed without one)
grep -qE '^SIGNAL_API_KEY(S)?=.+' .env && echo ok || echo "no key configured"

# 5. Suite green
cd backend && OPSEC_ENABLED=false .venv/bin/python -m pytest tests/ -q

# 6. Smoke green
./scripts/smoke.sh
```

---

## Related documents

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — how it is put together
- [`API.md`](API.md) — every endpoint
- [`SECURITY.md`](SECURITY.md) — what you are deploying, and its risks
- [`RUNBOOK.md`](RUNBOOK.md) — day-two operations
- [`CONTRACTS.md`](CONTRACTS.md) — frozen interface contracts