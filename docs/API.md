# Signal-OS API Reference

Every route in this document was verified against the running code: each path
below has a matching route decorator in `backend/api/*.py` or `backend/main.py`,
and the example bodies were captured from a live instance.

- **Base URL:** `http://<host>:8766` (`PORT` env var)
- **Interactive docs:** `/api/docs` when `DEBUG=true` (Swagger UI; `redoc` is off)
- **Wire format:** JSON, UTF-8. Report endpoints return `text/markdown`,
  `application/pdf` or `application/zip` depending on `?format=`.

---

## Contents

- [Authentication](#authentication)
- [Rate limiting](#rate-limiting)
- [Common error shapes](#common-error-shapes)
- [Investigations](#investigations)
- [Cases](#cases)
- [Agents](#agents)
- [Reports and exports](#reports-and-exports)
- [Ops and health](#ops-and-health)
- [OPSEC / Tor](#opsec--tor)
- [WebSocket](#websocket)

---

## Authentication

Every route except the WebSocket handshake is behind the auth gate installed by
`api/ops.guarded_router`. Two equivalent headers are accepted:

```
Authorization: Bearer <SIGNAL_API_KEY>
X-API-Key: <SIGNAL_API_KEY>
```

Configure keys in the environment:

```bash
# Single key, full access (scopes: *)
SIGNAL_API_KEY=your-key-here

# Multiple keys with scopes: label:key:scope1,scope2
SIGNAL_API_KEYS=ci:ci-key:read,search,ops analyst:a-key:investigate,read
```

Scopes: `investigate`, `search`, `read`, `write`, `opsec`, `admin`, `*`.

Auth is **on** unless `AUTH_DISABLED=true` or `ENVIRONMENT=development`. If auth
is enabled but no key is configured, every request is refused — the platform
fails closed rather than open.

---

## Rate limiting

Token bucket per `(principal, scope)`. Default budgets:

| Scope | Sustained RPM | Burst | Applies to |
|---|---|---|---|
| `investigate` | 6 | 2 | `/api/investigate*`, `/api/photo-search`, `/api/name-search` |
| `search` | 30 | 10 | `/api/search`, `/api/compare` |
| `read` | 240 | 120 | all GETs |
| `opsec` | 12 | 3 | `/api/opsec/newcircuit` |
| `websocket` | 30 | 10 | `/ws/pipeline/{id}` |

Every guarded response carries:

```
X-RateLimit-Limit: 6
X-RateLimit-Remaining: 4
X-RateLimit-Reset: 0
```

Exceeding a budget returns **429** with `Retry-After`. Override per scope with
`RATE_LIMIT_SCOPE_RPM.<scope>` and `RATE_LIMIT_BURST.<scope>`, or globally with
`RATE_LIMIT_RPM`. Buckets are **per worker process**, so `WORKERS=4` means the
effective ceiling is 4× the configured rate.

---

## Common error shapes

Authentication and rate-limit failures return structured JSON, never a
traceback:

```json
{"error": "invalid or missing API key", "auth": "failed", "required_scope": "investigate"}
```

Pydantic validation failures return **422** with FastAPI's detail array:

```json
{"detail": [{"type": "string_too_short", "loc": ["body", "input"], "msg": "String should have at least 1 character"}]}
```

Note: request models set `extra="forbid"`, so an unknown field is a 422 rather
than being silently ignored.

---

## Investigations

### `POST /api/investigate` — queue an investigation

Fire-and-forget. Returns immediately; poll `GET /api/investigate/{inv_id}` for
the result, or subscribe to the WebSocket.

**Request**

```json
{
  "input": "example.com",
  "input_type": "domain",
  "case_id": "optional-existing-case"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `input` | string | yes | 1–4096 chars |
| `input_type` | enum | no | `auto` (default) or one of the `InputType` values |
| `case_id` | string | no | Folds the run into an existing case; a new id is minted when omitted |

**Response `200`**

```json
{"inv_id": "baf2789e", "case_id": "a9ffb950", "status": "queued"}
```

**Errors:** 422 invalid body · 429 budget exhausted · 401/403 auth

---

### `POST /api/investigate-sync` — run to completion

Blocks until APEX finishes and returns the whole report in one round trip.
Use this on serverless, where a background task cannot be relied on to outlive
the response. No WebSocket streaming on this path.

**Request:** identical to `/api/investigate`.

**Response `200`**

```json
{
  "inv_id": "a4818553",
  "case_id": "6a110ec7",
  "status": "done",
  "input": "example.com",
  "input_type": "domain",
  "agents": [ /* one record per activated agent — see below */ ],
  "report": {
    "summary": "Investigation complete — domain; 9 agents, confidence 45%",
    "input_type": "domain",
    "agents_activated": ["SCOUT", "SIGMA", "CRAWLER", "PRISM", "NEXUS", "KRONOS", "VAULT", "SENTINEL", "QUILL"],
    "entities": [],
    "signals": [],
    "graph": {"nodes": [], "edges": []},
    "timeline": [],
    "markdown": "# Intelligence Report — example.com\n\n...",
    "confidence": 0.451,
    "latency_s": 3.75
  }
}
```

Each entry in `agents`:

```json
{
  "agent": "NEXUS",
  "role": "Correlation Engine",
  "icon": "◈",
  "status": "done",
  "output": { "...": "agent-specific payload" },
  "confidence": 0.82,
  "reasoning": "correlated 6 signals",
  "entities_found": [],
  "signals": [],
  "latency_s": 0.21,
  "tokens_used": 0,
  "error": null,
  "steps": ["[NEXUS] correlating 0 entities + 6 signals"]
}
```

---

### `GET /api/investigate/{inv_id}` — fetch one run

**Response `200`** — the stored record:

```json
{
  "inv_id": "baf2789e",
  "case_id": "a9ffb950",
  "input": "example.com",
  "input_type": "domain",
  "status": "done",
  "steps": [],
  "agents": [ /* as above */ ],
  "report": { /* as above */ },
  "confidence": 0.451,
  "latency_s": 3.75,
  "routing_confidence": 0.9,
  "routing_reasoning": "explicit input_type: domain",
  "entities": [],
  "signals": [],
  "started_at": "2026-10-01T07:54:00Z",
  "ended_at": "2026-10-01T07:54:04Z",
  "created_at": "2026-10-01T07:54:00Z",
  "updated_at": "2026-10-01T07:54:04Z"
}
```

`status` is one of `queued`, `running`, `done`, `error`.

**Errors:** `404 {"detail": {"error": "not found"}}` · `422` malformed id

---

### `GET /api/investigations` — list runs

Query: `limit` (default 50, max 200), `offset`.

**Response `200`** — array, newest first:

```json
[
  {
    "inv_id": "baf2789e",
    "case_id": "a9ffb950",
    "status": "done",
    "input": "example.com",
    "input_type": "domain",
    "started_at": "2026-10-01T07:54:00Z",
    "ended_at": "2026-10-01T07:54:04Z"
  }
]
```

---

### `POST /api/search` — run SCOUT directly

**Request**

```json
{"query": "example.com", "engines": ["google", "bing", "yandex"], "use_dorks": true}
```

**Response `200`**

```json
{
  "query": "example.com",
  "agent": "SCOUT",
  "status": "done",
  "output": { "...": "search payload" },
  "confidence": 0.4,
  "reasoning": "",
  "latency_s": 0.9,
  "steps": ["[SCOUT] theHarvester: inactive"],
  "error": null
}
```

**Errors:** `400` when `query` is blank · 429 on `search` budget

---

### `POST /api/photo-search` — upload a photo

`multipart/form-data`.

| Field | Type | Notes |
|---|---|---|
| `file` | file | **required**; image content-type allowlist, magic-byte sniff, size cap |
| `case_id` | string | optional |
| `deep` | bool | default `false` |

Queues the IRIS deanonymisation pipeline. Uploads land in a per-case temp
directory that is deleted when the pipeline finishes.

**Response `200`:** `{"inv_id": "...", "case_id": "...", "status": "queued"}`

---

### `POST /api/name-search` — person-name sweep

**Request:** `{"name": "Jane Doe", "location": "Berlin", "case_id": null}`

**Response `200`:** `{"inv_id": "...", "case_id": "...", "status": "queued"}`

**Errors:** 422 invalid body · 429 `investigate` budget

---

### `POST /api/compare` — model arena

Runs one prompt across N model configurations.

**Request**

```json
{
  "prompt": "Summarise the signal set",
  "models": ["gemma2:9b", "qwen2.5:7b"],
  "temperature": 0.2,
  "system": null,
  "rounds": 1
}
```

`models` accepts up to 12 entries; `rounds` is 1–10; `temperature` is 0.0–2.0.

---

## Cases

### `GET /api/cases` — list

Query: `limit`, `offset`. **Response `200`**:

```json
{
  "items": [
    {"case_id": "6a110ec7", "name": "smoke-1", "target": null, "tags": [],
     "notes": null, "created_at": "...", "updated_at": "...",
     "investigation_count": 3}
  ],
  "total": 1, "limit": 50, "offset": 0
}
```

### `POST /api/cases` — create

**Request**

```json
{"name": "Operation Bluebird", "target": "example.com", "tags": ["phishing"], "notes": "free text"}
```

`name` is required (1–256 chars); `tags` max 50; `notes` max 8192.

**Response `200`**: `{"case": {"case_id": "6a110ec7", ...}}`

### `GET /api/cases/{case_id}` — read
### `PATCH /api/cases/{case_id}` — update

Any subset of `name`, `target`, `tags`, `notes`. **Response `200`**: the updated case.

### `DELETE /api/cases/{case_id}` — delete (GDPR erasure path)

Deletes the case **and every investigation filed under it**, in one transaction.

**Response `200`**: `{"deleted": true, "case_id": "6a110ec7"}`

**Errors:** `404` unknown case

### `GET /api/cases/{case_id}/stats`
`{"investigations": 3, "entities": 12, "signals": 40, "reports": 3, ...}`

### `GET /api/cases/{case_id}/entities`
`{"items": [...], "total": 12, "limit": 100, "offset": 0}`

### `GET /api/cases/{case_id}/timeline`
Array of dated events reconstructed by KRONOS.

### `POST /api/cases/{case_id}/export` — ZIP bundle

Query: `include_raw` (bool, default `false`).

**Response `200`** — `application/zip`, assembled in memory, containing:

```
case.json  entities.json  signals.json  timeline.json
graph.json  investigations.json  reports/<inv_id>.md  README.md
```

`include_raw=true` embeds each investigation's full stored record rather than
just its index fields.

> Note the method and parameter: this is `POST` with `include_raw`. The
> *reports* router's export uses `GET` with `bundle=true` — they are different
> endpoints.

---

## Agents

### `GET /api/agents` — registry

Query: `limit` (default 100), `offset`. **Response `200`**:

```json
{
  "items": [
    {
      "name": "APEX", "role": "Master Supervisor", "icon": "⊕",
      "description": "Routes investigations, orchestrates agent swarms, synthesizes reports",
      "models": ["qwen2.5:7b", "gemma2:9b", "claude-opus-4"],
      "context_folders": [], "token_budget": 16384,
      "tier": "supervisor", "timeout_s": 180.0,
      "correlation_tier": ["NEXUS", "KRONOS", "VAULT", "SENTINEL", "QUILL"],
      "status": "idle"
    }
  ],
  "total": 16, "limit": 100, "offset": 0
}
```

### `GET /api/agents/registry/graph` — routing DAG

**Response `200`**: `{"nodes": [...], "edges": [...], "input_types": [...]}`
where each node is `{id, label, role, icon, tier, kind}` and each edge is
`{source, target, label, type, weight}`.

### `GET /api/agents/{name}` — one descriptor

**Errors:** `404 {"detail": {"error": "unknown agent", "name": "NOPE", "known_agents": "APEX, CRAWLER, ..."}}`

### `POST /api/agents/{name}/run` — run one agent

Runs real work: connectors, models, network. **Request**:

```json
{"input": "example.com", "input_type": "domain", "case_id": null,
 "deep": false, "lang": null, "context": {}}
```

**Response `200`**: a single agent result (same shape as one `agents[]` entry).

---

## Reports and exports

### `GET /api/investigate/{inv_id}/report` — render a report

Query: `format` ∈ `markdown` | `json` | `pdf`.

| `format` | `Content-Type` | Body |
|---|---|---|
| `markdown` (default) | `text/markdown` | `# Intelligence Report — …` |
| `json` | `application/json` | full structured report |
| `pdf` | `application/pdf` | rendered PDF (`%PDF` magic) |

**Errors:** `400` unknown format · `404` unknown investigation · `409` still
running · `501` when `FEATURE_PDF_EXPORT=false`

### `GET /api/investigate/{inv_id}/export` — download an artefact

Query: `format` (`markdown` default, or `json`, `pdf`) and `bundle` (bool).

With `bundle=true` the response is `application/zip` containing `report.md`,
`report.json`, `agents.json`, `steps.json` and `report.pdf` in one archive.
Otherwise it is the single artefact with a
`Content-Disposition: attachment; filename="signal-os-<inv_id>.md"` header.

**Errors:** same as `/report`, plus 400 on an unknown format.

---

## Ops and health

### `GET /api/health` — liveness

Cheap, never blocks. **Response `200`**:

```json
{
  "status": "degraded",
  "platform": "signal-os",
  "version": "0.1.0",
  "uptime_s": 4.02,
  "llm": {"available": false, "provider": null, "model": null, "error": "no provider configured"},
  "store": {"backend": "memory", "degraded": true, "investigations": 0, "cases": 0, "reports": 0, "audit_events": 0},
  "cache": {"backend": "memory", "hit_rate": 0.0, "entries": 0},
  "agents": {"total": 16, "healthy": 16}
}
```

`status` is `ok`, `degraded` (a dependency fell back) or `down`. **`degraded`
is the expected state with zero infrastructure running** — the store degrades to
memory and the cache to an in-process LRU. See
[`RUNBOOK.md` — Degraded store](RUNBOOK.md#the-store-reports-degraded-mode).

### `GET /api/health/deep` — dependency probes

Probes every backing service concurrently with per-service timeouts.

**Response `200`**:

```json
{
  "status": "degraded",
  "checks": {
    "postgres": {"ok": false, "latency_ms": 0, "detail": "connection refused"},
    "redis":    {"ok": false, "latency_ms": 0},
    "qdrant":   {"ok": false},
    "neo4j":    {"ok": false},
    "minio":    {"ok": false}
  },
  "store": {"...": "..."},
  "llm": {"available": false}
}
```

### `GET /api/metrics`

In-process counters and gauges as JSON. Disabled by `METRICS_ENABLED=false`.

### `GET /api/config`

Effective, redacted configuration: which env vars are *present* (booleans only,
never values) plus feature flags and timeouts.

**Response `200`**:

```json
{
  "version": "0.1.0", "environment": "development", "debug": true,
  "features": {"pdf_export": true, "email_enum": true, "dark_web_alerts": false},
  "env_presence": {"SIGNAL_API_KEY": false, "SHODAN_API_KEY": false, "LLM_ENABLED": true},
  "timeouts": {"APEX": 180.0, "SCOUT": 60.0}
}
```

---

## OPSEC / Tor

### `GET /api/opsec/status`

```json
{"opsec_enabled": true, "proxy": "socks5h://127.0.0.1:9050",
 "tor_connected": true, "current_ip": "<exit-node-ip>", "requests_routed": 128}
```

When the client is unavailable this returns `{"opsec_enabled": false, "error": "..."}`
with HTTP 200 — it never fails a request.

### `POST /api/opsec/newcircuit`

Sends `SIGNAL NEWNYM` to the Tor control port for a new exit node.

```json
{"success": true, "response": "250 OK"}
```

Returns `{"success": false, "error": "..."}` with HTTP 200 when rotation is
unavailable. Scope: `opsec` (12 rpm, burst 3).

---

## WebSocket

### `WS /ws/pipeline/{inv_id}`

Live pipeline trace for one investigation. Origin-checked against
`WS_ALLOWED_ORIGINS` (non-browser clients send no `Origin` and are allowed).

**Event frames**

```json
{"type": "step",   "inv_id": "baf2789e", "message": "[SCOUT] theHarvester: inactive"}
{"type": "done",   "inv_id": "baf2789e", "status": "done", "report": {…}, "agents": […], "latency_s": 3.75}
{"type": "ping",   "inv_id": "baf2789e", "ts": "2026-10-01T07:54:10Z"}
```

- The first frame is the first pipeline `step` — there is **no** `connected`
  handshake event.
- Server sends `{"type":"ping"}` after `WS_HEARTBEAT_S` of silence; a client
  `ping` is echoed with a matching `nonce`.
- Subscribing to an **already-finished** investigation replays a single `done`
  frame carrying the final report, so a reconnecting client converges without
  replaying every step.
- An unknown `inv_id` is accepted and simply streams nothing; the socket is a
  subscription endpoint, not an existence check.

```bash
websocat "ws://localhost:8766/ws/pipeline/baf2789e"
```

---

## Rate limits by endpoint

| Endpoint | Method | Scope |
|---|---|---|
| `/api/investigate`, `/api/investigate-sync`, `/api/photo-search`, `/api/name-search` | POST | `investigate` |
| `/api/search`, `/api/compare` | POST | `search` |
| `/api/opsec/newcircuit` | POST | `opsec` |
| all GET routes | GET | `read` |
| `/ws/pipeline/{id}` | WS | `websocket` |

---

## Endpoint count

Routers mounted by `main.py`: `api.ops`, `api.investigate`, `api.cases`,
`api.agents`, `api.reports`. `main.py` additionally registers a handful of
backwards-compatible routes the dashboard depends on at the same paths; the
mounted routers win any genuine collision.