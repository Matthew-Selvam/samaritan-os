# Signal-OS Architecture

This document describes the system as it actually is in the code. Every file
path, agent name, tier and DAG edge below was read from source; where the
implementation diverges from the intent, the code wins and the divergence is
called out.

---

## 1. What Signal-OS is

A multi-agent OSINT (open-source intelligence) platform. One input — a
username, domain, phone number, email, photo, file hash or block of text — is
routed to a swarm of specialist agents that reach outward through connectors,
extract signals, correlate them into a graph, reconstruct a timeline, and
synthesise a written intelligence report.

Three properties shape every design decision:

1. **It must run with zero infrastructure.** Postgres, Redis, Qdrant, Neo4j,
   MinIO, SearXNG, SpiderFoot, an LLM provider and Tor are *all* optional.
   Every one has a working fallback. `scripts/smoke.sh` runs the entire
   pipeline on a laptop with nothing else running.
2. **A missing dependency must degrade, never break.** An unconfigured
   connector returns a structured `{"error": ...}` dict, never an exception
   that takes down a pipeline stage.
3. **The AI tier is optional and opt-in.** With `LLM_ENABLED=false` every agent
   runs a deterministic path. The platform stays fully functional — it produces
   machine-derived output instead of AI-synthesised narrative.

---

## 2. System diagram

```
                         ┌──────────────────────────┐
   client ──HTTP/WS──►   │  main.py  FastAPI app    │
                         │  CORS · GZip · lifespan  │
                         └────────────┬─────────────┘
                                      │ include_router (defensive)
        ┌──────────────┬──────────────┼───────────────┬──────────────┐
        ▼              ▼              ▼               ▼              ▼
  ┌──────────┐  ┌───────────┐  ┌────────────┐  ┌──────────┐  ┌──────────┐
  │api.ops   │  │api.invest │  │api.cases   │  │api.agents│  │api.report│
  │health    │  │igate      │  │CRUD+zips   │  │registry  │  │md/json/pdf│
  │metrics   │  │submit/poll│  │            │  │+rundag   │  │          │
  │config    │  │search     │  └────────────┘  └──────────┘  └──────────┘
  │opsec     │  │photo/name │        ▲              ▲              ▲
  │WS hub    │  └─────┬─────┘        └──────────────┴──────────────┘
  └────┬─────┘        │
       │              ▼
       │      ┌───────────────┐   auth + scope + token bucket
       │      │  runtime.py   │   (api/deps.py on every guarded route)
       │      │  store handle │
       │      └───────┬───────┘
       │              ▼
       │      ┌───────────────┐
       └─────►│  APEX  DAG    │  16 agents, staged, concurrent within a stage
              │  (agents/)    │
              └───────┬───────┘
                      │  each agent imports its own connectors lazily
                      ▼
       ┌──────────────────────────────────────────────────────┐
       │  connectors/  — 19 registered run_* entry points    │
       │  net · imgguard · langid · exif · browser · transcribe│
       │  sherlock · shodan · searxng · spiderfoot ·          │
       │  theharvester · breach_check · email_enum ·           │
       │  people_search · phone_intel · reverse_image ·        │
       │  bioclip · plantnet · face_embed · qdrant_* · darkweb│
       └───────┬───────────────────────────────┬──────────────┘
               │                               │
               ▼                               ▼
      ┌─────────────────┐          ┌────────────────────────┐
      │ security.py     │          │ opsec.py (Tor)         │
      │ SSRF guard      │          │ socks5h proxy + NEWNYM │
      │ safe_join       │          └────────────────────────┘
      └─────────────────┘
```

Cross-cutting layers, each with a fallback:

| Layer | Module | Primary | Fallback |
|---|---|---|---|
| Storage | `store.py` | Postgres / SQLite | in-memory (degraded) |
| Cache | `cache.py` | Redis | in-process TTL-LRU |
| LLM | `llm.py` | openai → anthropic → deepseek → openrouter | deterministic agent path |
| Resilience | `circuit.py` | circuit breakers per dependency | fail open, log |
| Secrets | `auth.py` + `api/deps.py` | API keys + scopes | — |
| Metrics | `observability.py` | in-process registry | `METRICS_ENABLED=false` |

---

## 3. The agent tiers

`agents/AGENT_REGISTRY` holds **16** agents.

**Supervisor (1)**

| Agent | Role |
|---|---|
| `APEX` | Master Supervisor — routes, schedules the DAG, synthesises the report. |

**Primary swarm (10)** — activated by input type, run concurrently.
Read from `config.PRIMARY_AGENTS`.

| Agent | Role (`role` attribute) |
|---|---|
| `SCOUT` | Search Intelligence |
| `SIGMA` | Threat Intelligence |
| `PHONOS` | Phone Intelligence |
| `EMAIL` | Email Intelligence |
| `CRAWLER` | Web Scraper |
| `IRIS` | Vision Intelligence |
| `ECHO` | Audio Intelligence |
| `PRISM` | Social Intelligence |
| `TERRA` | GEOINT |
| `INK` | Stylometry |

**Correlation tier (5)** — `config.CORRELATION_TIER`. Always activated.

| Agent | Role (`role` attribute) |
|---|---|
| `NEXUS` | Correlation Engine |
| `KRONOS` | Timeline Reconstruction |
| `VAULT` | Memory Agent |
| `SENTINEL` | Live Monitoring |
| `QUILL` | Report Generation |

QUILL is `_TERMINAL` — it synthesises and never runs concurrently with others.

### Routing

`router.py` maps an `InputType` to its primary swarm via `AGENT_MAP`:

```python
InputType.DOMAIN:  ["SCOUT", "SIGMA", "CRAWLER", "PRISM"]
InputType.USERNAME:["SCOUT", "PRISM", "SIGMA"]
InputType.EMAIL:   ["EMAIL", "SCOUT", "SIGMA", "PRISM"]
InputType.PHONE:   ["PHONOS", "SIGMA", "SCOUT"]
InputType.URL:     ["CRAWLER", "IRIS", "SIGMA", "PRISM"]
InputType.IP:      ["SIGMA", "TERRA", "CRAWLER"]
```

A high-confidence table hit is trusted outright (`LLM_DISAMBIGUATION_THRESHOLD
= 0.6`); below `UNKNOWN_CONFIDENCE_CEILING = 0.45` the LLM is consulted for
disambiguation. The correlation tier is appended regardless of input type.

---

## 4. The DAG

`ApexAgent.DAG` — stages run in order; every agent **inside** a stage runs
concurrently:

```
  input ──► APEX routes + activates the primary swarm
                            │
                            ▼
              ┌───────────────────────────┐
              │ PRIMARY SWARM (parallel)   │
              │ SCOUT SIGMA CRAWLER PRISM │
              │ (type-dependent)          │
              └─────────────┬─────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                                       ▼
┌────────────────┐                    ┌──────────────────┐
│ correlate      │                    │  (stage 1+2 run │
│ NEXUS ‖ KRONOS │                    │   in order)      │
└───────┬────────┘                    └──────────────────┘
        │
        ▼
┌────────────────┐
│ observe        │
│ VAULT ‖ SENTINEL│
└───────┬────────┘
        │
        ▼
┌────────────────┐
│ report         │
│ QUILL (alone)  │
└───────┬────────┘
        │
        ▼
   synthesis ──► report + confidence
```

Concretely, `ApexAgent.DAG` is three stages:

```python
DAG = (
    ("correlate", ("NEXUS", "KRONOS")),   # both read only the primary swarm
    ("observe",   ("VAULT", "SENTINEL")), # both read everything so far
    ("report",    ("QUILL",)),            # needs the finished aggregate
)
```

The ordering is load-bearing: a correlation agent that ran before collection
would correlate an empty set. QUILL is `_TERMINAL` and runs alone.

**Failure semantics.** Each agent gets its own timeout (`config.TIMEOUT_*`;
APEX enforces a 180 s overall deadline). Transient failures —
`asyncio.TimeoutError`, `ConnectionError`, `OSError` — are retried
(`ApexAgent.RETRYABLE`); everything else fails fast. A failed agent is recorded
with `status="error"` and the pipeline continues. Confidence is penalised for
partial results, so a degraded run reports lower confidence rather than
pretending.

Measured, with no infrastructure running: **9 agents in 2.4–3.9 s** (p50 2.75 s,
p95 3.91 s over 6 runs — see `scripts/bench.sh`).

---

## 5. Request lifecycle

### The async path (`POST /api/investigate`)

```
1. api/deps      authenticate → authorise scope → rate limit
2. schemas.py    validate (extra="forbid", every field length-capped)
3. store         save_investigation(status="queued", report=None)
4. respond       {"inv_id", "case_id", "status": "queued"}   ← returns NOW
5. asyncio       run_pipeline_background(...)  — fire and forget
6. router        classify input type → AGENT_MAP → primary swarm
7. APEX          run stages; each agent's log() fans out to the WS hub
8. store         patch record: status=done, agents[], report, latency_s
9. WS clients    receive {"type":"done", report, agents}
```

Step 4 returning before step 5 is the entire point of this endpoint: the client
gets an id immediately and follows progress over the socket or by polling.

### The sync path (`POST /api/investigate-sync`)

Identical, but step 4 is skipped — the request blocks until the pipeline
finishes and returns the report in one round trip. This exists because
serverless platforms isolate each invocation: a background task cannot be
relied on to outlive the response, and a later `GET` may land on a different
instance. See [`DEPLOY.md` — Vercel](DEPLOY.md#5-vercel).

---

## 6. The layers in detail

### `store.py` — persistence

Three backends behind one async interface: Postgres, SQLite, and in-memory.

- `STORE_BACKEND=auto` (default) probes Postgres and falls back to SQLite, then
  to memory.
- **Degraded mode is the designed behaviour**, not an error: the app logs
  `store degraded to memory`, keeps serving, and `/api/health` reports
  `store.degraded=true`.
- `STORE_MAX_ROWS` (default 5000) caps each collection, evicting oldest first.
  This is the default retention bound.
- `delete_case(case_id)` removes the case **and** every investigation under it
  in one transaction — the GDPR erasure path
  ([`SECURITY.md`](SECURITY.md#9-gdpr-and-data-retention)).
- Every mutating route writes an audit event (`store.audit`), best-effort.

### `cache.py` — Redis with a real fallback

`REDIS_URL` is tried once at startup; a refused connection falls back to an
in-process TTL-LRU (`CACHE_MAX_SIZE`, `CACHE_TTL_SECONDS`). Setting
`REDIS_REQUIRED=true` turns the fallback off so a missing Redis is a hard
failure instead.

### `llm.py` — the provider chain

Providers are tried in `LLM_PROVIDER_ORDER`. **Local Ollama is opt-in**: it
requires `LLM_ALLOW_LOCAL=true` *and* `ollama` in the provider order. A daemon
on the default port is deliberately not treated as consent — loading
multi-gigabyte weights is slow, and on a memory-pressured host every agent's AI
pass becomes a timeout that stalls the whole pipeline.

`LLM_ENABLED=false` disables the tier entirely; agents log
`llm offline fallback (reason=no LLM provider available)` and use their
deterministic path.

### `security.py` — SSRF and path safety

The most important module for a platform that fetches attacker-supplied URLs.

- `ip_is_blocked()` rejects loopback, link-local, private, reserved and
  CGNAT ranges for both IPv4 and IPv6 — including the cloud metadata address
  `169.254.169.254`.
- The check is applied **to resolved addresses, not to the hostname string**,
  which closes the DNS-rebinding hole where a name resolves public during
  validation and private during the actual connection.
- `safe_join(root, *parts)` guarantees a resolved path stays inside `root`.
- `sanitize_filename()` strips traversal and shell metacharacters from
  uploads.
- Redirects are followed manually with a hop limit, and hop-by-hop headers plus
  `Authorization` are stripped on a cross-host hop (`_strip_hop_secrets`).
- Streaming reads are capped, so a decompression bomb cannot exhaust memory.
- Network and SSRF failures return `{"error": ...}` rather than raising —
  a crawler must never take down a pipeline.

`ALLOW_PRIVATE_NETWORK=true` disables the private-range checks. It is required
for the docker-compose stack, where `searxng` and `spiderfoot` are private
service names. **Never enable it on a host that can reach cloud metadata.**

### `opsec.py` — the Tor layer

When `OPSEC_ENABLED=true`, outbound HTTP goes through `TOR_PROXY`
(default `socks5h://127.0.0.1:9050`). `socks5h` resolves DNS through Tor, so
the target hostname never hits the local resolver.

Control port `TOR_CONTROL` (default `127.0.0.1:9051`) accepts
`SIGNAL NEWNYM` for circuit rotation, exposed as `POST /api/opsec/newcircuit`.
See [`RUNBOOK.md` — Rotate the Tor circuit](RUNBOOK.md#rotate-the-tor-circuit).

### `rate_limit.py` — token buckets

Per `(principal, scope)`. Scopes exist because a uniform limit is useless:
`POST /api/investigate` fans out to ~10 agents and dozens of outbound calls,
while `GET /api/health` is nearly free. Limiting them together would either
break the app or break the health check. Buckets are in-process, so with
`WORKERS=4` the effective ceiling is 4× the configured rate — see
[`API.md` — Rate limiting](API.md#rate-limiting).

### `connectors/__init__.py` — the registry

19 logical entries mapping to the `run_*` coroutines in the connector modules.
Every entry is a **lazy proxy**: the connector module is imported on first
call, so a missing optional dependency (Playwright, faster-whisper,
qdrant-client) can never break `import connectors`.

`connector_health()` reports, per connector, whether it is `available`
(importable) and `installed` (dependencies present), what it `requires`, and a
human-readable `notes` explaining any degradation. It performs **import probes
only** — no network I/O — so it is safe on a health endpoint and in CI.
`tests/test_smoke.py` re-derives the `run_*` set from the source with `ast` and
fails if the registry and the tree ever disagree.

---

## 7. WebSocket streaming

`api/ops.py` keeps the pipeline socket on a **second** router, `ws_router`, with
no prefix. Every other router is mounted with a bare `include_router(...)`; the
socket must stay at `/ws/pipeline/{inv_id}` for the dashboard's benefit, and
mixing prefixed and unprefixed mounts is exactly how that breaks.

`WS_HUB` fans a single pipeline's events out to every subscriber. APEX agents
attach an `asyncio.Queue` via `attach_stream`; each `log()` call schedules a
delivery, so a dead client cannot stall or kill a run.

- Origin-checked against `WS_ALLOWED_ORIGINS`. Non-browser clients send no
  `Origin` and are allowed through — they still cleared the ASGI layer.
- Subscribing to a finished investigation replays one `done` frame, so a
  reconnecting client converges without replaying every step.
- `{"type":"ping"}` keepalives every `WS_HEARTBEAT_S` of silence.
- Every exit path unsubscribes; no channel entry outlives its connection.

---

## 8. Deployment topology

```
                      ┌─────────────────────┐
  browser ───────────►│  frontend (Next.js) │
                      └──────────┬──────────┘
                                 │ NEXT_PUBLIC_API_URL
                                 ▼
   ┌──────────────────────────────────────────────────────────┐
   │  app tier                                                │
   │  ┌────────────┐   ┌──────────────┐   ┌────────────────┐  │
   │  │ backend    │   │ celery-worker│   │ celery-beat    │  │
   │  │ supervisord│   │ N replicas   │   │ scheduled      │  │
   │  │ :8766      │   │ concurrency=4│   │ tasks          │  │
   │  └─────┬──────┘   └──────┬───────┘   └────────────────┘  │
   └────────┼─────────────────┼──────────────────────────────┘
            │                 │
   ┌────────┴─────────────────┴────────────────────────┐
   │  data tier (optional — every one has a fallback) │
   │  Postgres · Redis · Neo4j · Qdrant · MinIO      │
   └─────────────────────────────────────────────────┘
            │
   ┌────────┴─────────┐
   │  OSINT tier      │
   │  SearXNG · SpiderFoot · theHarvester · Sherlock  │
   │  Tor (optional)  │
   └──────────────────┘
```

`docker-compose.yml` brings up four tiers: data (postgres, redis, neo4j,
qdrant, minio), app (backend, frontend), workers (celery-worker, celery-beat)
and OSINT (searxng, tor).

The Dockerfile runs `supervisord` under `tini`, so one container serves both
the API and the Celery worker. Health is `/api/health` with a
`healthcheckTimeout` of 120 s, because the app probes its dependencies on
startup and that legitimately takes a moment when services are slow.

### The scaling constraint that shapes everything

**An investigation is a multi-second, multi-request, stateful unit of work.** It
cannot be split across instances, and a serverless function cannot hold it open
past its execution cap. Therefore:

- **Long-running work belongs on Celery** (`celery-worker` with
  `concurrency=4`, `CELERY_ENABLED`). The API returns an id immediately.
- **Serverless can only host the sync path**, where one request does the whole
  thing inside its own execution window.
- **Rate-limit state is per-process**, so horizontal scaling multiplies the
  effective ceiling until the limiter is moved to Redis-backed counting.

[`DEPLOY.md`](DEPLOY.md) works through each target honestly, including where
the platform does not fit.

---

## 9. Request-path module map

| Path | Responsibility |
|---|---|
| `backend/main.py` | App wiring, middleware, lifespan, defensive router mounting |
| `backend/api/deps.py` | auth + scope + rate-limit dependencies; id validation |
| `backend/api/ops.py` | health, deep health, metrics, config, OPSEC, WS hub |
| `backend/api/investigate.py` | submit, poll, sync, search, photo, name, compare |
| `backend/api/cases.py` | case CRUD, stats, entities, timeline, ZIP export |
| `backend/api/agents.py` | registry, routing graph, single-agent run |
| `backend/api/reports.py` | report render (md/json/pdf) and export artefacts |
| `backend/runtime.py` | pipeline run/stream, store handle, ws registration |
| `backend/router.py` | input classification and `AGENT_MAP` |
| `backend/schemas.py` | strict request models (`extra="forbid"`, length caps) |
| `backend/store.py` | Postgres / SQLite / memory persistence |
| `backend/cache.py` | Redis with in-process fallback |
| `backend/llm.py` | provider chain and circuit breaking |
| `backend/security.py` | SSRF guard, safe path join, safe fetch |
| `backend/opsec.py` | Tor proxy, circuit rotation, status |
| `backend/auth.py` | API keys, scopes, failure throttling |
| `backend/rate_limit.py` | per-(principal, scope) token buckets |
| `backend/circuit.py` | per-dependency circuit breakers |
| `backend/agents/` | the 16 agents; `apex.py` holds the DAG |
| `backend/connectors/` | 19 external source connectors + the registry |
| `backend/celery_app.py`, `tasks.py` | background execution |

---

## 10. Related documents

- [`API.md`](API.md) — every endpoint, verified against route decorators
- [`DEPLOY.md`](DEPLOY.md) — local, Compose, Railway, Vercel, bare VPS
- [`SECURITY.md`](SECURITY.md) — threat model, auth, SSRF, OPSEC, GDPR
- [`RUNBOOK.md`](RUNBOOK.md) — operational procedures
- [`CONTRACTS.md`](CONTRACTS.md) — the frozen interface contracts