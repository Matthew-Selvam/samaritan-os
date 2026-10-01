# Signal-OS — Internal Interface Contracts (authoritative)

> Every workstream builds against these exact signatures. **Do not change a
> signature** that another workstream depends on. If you genuinely need a
> different shape, add an *additional* function rather than renaming.
>
> Spec source of truth: `README.md` (features) + `CLAUDE.md` (stack/agents).
> **Nothing in the spec may be removed.** All 15 agents, all connectors, all
> documented endpoints, all env vars must survive this refactor.

---

## 0. Repo layout (final)

```
backend/
  main.py                 # FastAPI app — owned by WS-MAIN only
  config.py               # centralized env — WS-MAIN only (append-only)
  security.py             # WS-SEC
  llm.py                  # WS-LLM  (single entry point for ALL model calls)
  cache.py                # WS-INFRA
  store.py                # WS-INFRA
  tasks.py                # WS-INFRA (celery tasks)
  celery_app.py           # WS-INFRA
  observability.py        # WS-INFRA (logging, metrics, redaction)
  circuit.py              # WS-INFRA (breaker + retry helpers)
  schemas.py              # WS-SEC  (pydantic request/response models)
  rate_limit.py           # WS-SEC
  auth.py                 # WS-SEC
  api/
    deps.py               # WS-SEC
    investigate.py        # WS-API
    agents.py             # WS-API
    cases.py              # WS-API
    ops.py                # WS-API
    reports.py            # WS-API
  agents/                 # base.py + one module per agent
  connectors/             # one module per source
frontend/
  app/                    # Next 16 App Router (Tailwind 4) — canonical UI
  components/
  lib/                    # shared TS clients/hooks/types — WS-FE-*
```

Root `pages/index.tsx` + root MUI `package.json` are **legacy duplicates** of the
real app in `frontend/`. WS-MAIN deletes them (not spec content).

---

## 1. `backend/llm.py` — WS-LLM (owned)

Single entry point for every model call. No other module may talk to a model
provider directly.

```python
class LLMUnavailable(RuntimeError): ...

@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    error: str | None = None

class LLM:
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResult: ...

    async def complete_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        schema_hint: str | None = None,
    ) -> dict | list: ...

    async def stream(self, prompt: str, **kw):  # async generator of str
        ...

    def available_models(self) -> list[str]: ...
    def health(self) -> dict: ...

def get_llm() -> LLM: ...          # process-wide singleton
async def close_llm() -> None: ... # shutdown
```

Rules:
- Provider chain resolved from env, first healthy wins, automatic failover:
  `OPENAI_API_KEY` → `ANTHROPIC_API_KEY` → `DEEPSEEK_API_KEY` →
  `OPENROUTER_API_KEY` → local Ollama (`OLLAMA_URL`, default
  `http://localhost:11434`) → **deterministic offline fallback** (must never
  raise when no provider is configured — return an `LLMResult` whose `error` is
  set and `text` is a templated non-AI answer).
- Circuit-break a provider after N consecutive failures (N from env).
- Every call honours `BaseAgent.token_budget` via `max_tokens`.
- **Never log prompt/response bodies** — log token counts + model only.

## 2. `backend/agents/base.py` — WS-MAIN (append-only)

Existing contract is unchanged:

```python
@dataclass
class AgentResult:
    agent: str
    status: str                 # done | error | partial | skipped
    output: Any
    confidence: float = 0.0
    reasoning: str = ""
    entities_found: list[dict] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)
    latency_s: float = 0.0
    tokens_used: int = 0
    error: str | None = None

class BaseAgent(ABC):
    name / role / icon / description: str
    preferred_models: list[str]
    context_folders: list[str]
    token_budget: int
    def log(self, msg: str) -> None
    def attach_stream(self, queue) -> None
    async def run(self, input_data: str | dict, context: dict | None = None) -> AgentResult
    def _start_timer() / _elapsed(t0)
```

Extensions added by WS-MAIN — **read them, don't redefine them**:

```python
    # in AgentResult
    def to_dict(self) -> dict: ...

    # in BaseAgent
    async def llm(self, prompt, **kw): ...   # -> LLMResult; uses get_llm()
    async def llm_json(self, prompt, **kw): ...
    def timeout(self) -> float: ...           # -> config.get_timeout(self.name)
```

`context` dict keys an agent may rely on (all optional):
`emit`, `input_type`, `case_id`, `peer_signals`, `peer_entities`,
`agent_results`, `deep`, `lang`.

## 3. Entity & signal shapes (canonical — NEXUS/frontend depend on these)

```python
# entity
{"id": str,            # stable, deterministic: f"{type}:{normalized_value}"
 "label": str,
 "type": str,          # person|email|username|domain|url|ip|phone|account|
                       # image|face|location|wallet|org|crypto|document|note|other
 "value": str,
 "confidence": float,
 "source": str,        # agent name
 "first_seen": str,    # ISO 8601
 "last_seen": str,
 "attrs": dict}

# signal
{"type": str,          # e.g. account_found, breach, geolocation, dork, ...
 "value": Any,
 "source": str,
 "confidence": float,  # 0..1 — ALWAYS present
 "ts": str | None,     # ISO 8601 if the signal carries a date
 "url": str | None,
 "attrs": dict}

# graph
{"nodes": [ {"id","label","type","weight","confidence","group"} ],
 "edges": [ {"source","target","label","type","weight","confidence"} ],
 "clusters": [ {"id","label","size","members":[node_id,...]} ]}

# timeline event
{"id","ts","date","label","description","source","confidence","entities":[id,...]}
```

Type colours + the canonical type list live in
`frontend/lib/entityTypes.ts` (WS-FE owns it). Backend `router.py` may only use
types from the list above.

## 4. `backend/store.py` — WS-INFRA (owned)

```python
class Store(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...

    # investigations
    async def save_investigation(self, rec: dict) -> None: ...
    async def update_investigation(self, inv_id: str, patch: dict) -> None: ...
    async def get_investigation(self, inv_id: str) -> dict | None: ...
    async def list_investigations(self, *, case_id: str | None = None,
                                   limit: int = 100) -> list[dict]: ...
    async def append_step(self, inv_id: str, step: str) -> None: ...

    # cases
    async def upsert_case(self, case: dict) -> dict: ...
    async def get_case(self, case_id: str) -> dict | None: ...
    async def list_cases(self, limit: int = 100) -> list[dict]: ...
    async def delete_case(self, case_id: str) -> bool: ...
    async def case_stats(self, case_id: str) -> dict: ...

    # reports
    async def save_report(self, inv_id: str, md: str, meta: dict) -> str: ...

    # audit
    async def audit(self, event: dict) -> None: ...

def get_store() -> Store: ...
```

Backends: SQLite (`aiosqlite`, default, zero-config) → Postgres when
`DATABASE_URL` is set. Must create schema on `start()` (idempotent
`CREATE TABLE IF NOT EXISTS`). Never raise on DB failure — degrade to an
in-memory dict and set `store.degraded = True`.

## 5. `backend/cache.py` — WS-INFRA (owned)

```python
def cached(ttl: float = 300.0, key: str | None = None): ...   # decorator
async def cache_get(key: str) -> Any | None: ...
async def cache_set(key: str, value: Any, ttl: float | None = None) -> None: ...
async def cache_delete_prefix(prefix: str) -> int: ...
def cache_stats() -> dict: ...
```

Redis when `REDIS_URL` reachable, else TTL-LRU in-process dict. Both paths must
be exception-proof.

## 6. `backend/circuit.py` — WS-INFRA (owned)

```python
class CircuitOpen(Exception): ...

class CircuitBreaker:
    def __init__(self, name: str, fail_threshold: int = 5,
                 reset_after: float = 30.0): ...
    def allow(self) -> bool: ...
    def record_success(self) -> None: ...
    def record_failure(self, exc: BaseException | None = None) -> None: ...

async def retry(fn, *, attempts: int = 3, backoff: float = 0.5,
                exceptions: tuple = (Exception,)) -> Any: ...
```

## 7. `backend/observability.py` — WS-INFRA (owned)

```python
def get_logger(name: str) -> logging.Logger: ...
def redact(obj: Any) -> Any: ...          # deep mask of secrets/PII-ish keys
REDACT_KEYS: frozenset[str]
class Metrics:
    def incr(self, name: str, value: int = 1, **tags) -> None: ...
    def timing(self, name: str, seconds: float, **tags) -> None: ...
    def gauge(self, name: str, value: float, **tags) -> None: ...
    def snapshot(self) -> dict: ...
def get_metrics() -> Metrics: ...
```

`REDACT_KEYS` must include: `apikey, api_key, password, secret, token,
authorization, cookie, ssh_key, sessionid, session_id`.

## 8. Security — WS-SEC (owned)

### `backend/auth.py`
```python
class AuthError(Exception): ...
def require_api_key(request: Request) -> str:   # returns the key's id/label
def auth_enabled() -> bool: ...                  # False only when AUTH_DISABLED=true
def principal(request: Request) -> dict: ...     # {"id","scopes","label"}
def require_scope(scope: str): ...               # dependency factory
```

Auth method: `Authorization: Bearer <SIGNAL_API_KEY>` or `X-API-Key: <key>`.
`SIGNAL_API_KEYS` (comma-separated `label:key` pairs) supports multiple keys +
scopes (`label:key:scope1,scope2`). Anonymous access allowed **only** when
`AUTH_DISABLED=true` or `ENVIRONMENT=development`; in that case principal is
`{"id":"anonymous","scopes":["*"]}`. Log every auth failure with IP+path.

### `backend/security.py`
```python
BLOCKED_SCHEMES = {"file","gopher","ftp","data","javascript","ldap"}
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_BYTES   = 50 * 1024 * 1024

def is_safe_url(url: str) -> bool: ...        # scheme + host validation
async def assert_public_host(host: str) -> None:
    """Raise SSRFError if host resolves to loopback/private/link-local/
    reserved/multicast/CGNAT/cloud-metadata (169.254.169.254, fd00::/8,
    metadata.google.internal). Must resolve DNS and check EVERY A/AAAA record."""
class SSRFError(ValueError): ...

def sanitize_filename(name: str) -> str: ...
def safe_join(root: str, *parts: str) -> str: ...   # raises on traversal
def rate_limit_headers(...) -> dict: ...
```

**SSRF is the top security hole in this codebase**: `CrawlerAgent` fetches
arbitrary user-supplied URLs from the server. Every outbound fetch path must go
through `assert_public_host` + response-size cap + `BlockPrivateNetworks`.

### `backend/rate_limit.py`
```python
class RateLimiter:
    def __init__(self, key: str, rate: float, burst: int): ...
    def allow(self) -> tuple[bool, float]: ...   # (allowed, retry_after_s)
def get_limiter(scope: str) -> RateLimiter: ...  # per-scope limits from env
```

### `backend/schemas.py`
Pydantic v2 models for every request body, with `Field(max_length=...)` on all
free-text input, `extra="forbid"` on request models, and strict types. Exports
`InvestigateRequest`, `SearchRequest`, `NameSearchRequest`, `CaseCreate`,
`CaseUpdate`, `CompareRequest`, `SaveMemoryRequest`, `ReportFormat`.

### `backend/api/deps.py`
```python
def require_auth_dep = Depends(require_api_key)
def principal_dep = Depends(principal)
async def inv_id_path(inv_id: str) -> str: ...   # validates id format
```

### Env vars added (all optional, safe defaults)
`SIGNAL_API_KEY`, `SIGNAL_API_KEYS`, `AUTH_DISABLED`, `CORS_ORIGINS`,
`RATE_LIMIT_RPM`, `RATE_LIMIT_BURST`, `WS_ALLOWED_ORIGINS`,
`MAX_UPLOAD_BYTES`, `ALLOW_PRIVATE_NETWORK=false`, `AUDIT_LOG=true`.

## 9. `backend/tasks.py` + `celery_app.py` — WS-INFRA (owned)

```python
# celery_app.py
celery_app = Celery("signal_os", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(task_serializer="json", task_acks_late=True,
                       worker_prefetch_multiplier=1, task_time_limit=600,
                       task_soft_time_limit=540)

# tasks.py
@celery_app.task(bind=True, name="signal_os.investigate", max_retries=2)
def run_investigation(self, payload: dict) -> dict: ...

@celery_app.task(name="signal_os.sentinel_scan")
def sentinel_scan(case_id: str, target: str) -> dict: ...

@celery_app.task(name="signal_os.report")
def build_report(inv_id: str, fmt: str = "markdown") -> dict: ...
```

Each task must also run **in-process** when Redis/Celery is unavailable — expose
`async def run_investigation_local(payload: dict) -> dict` and have the route
prefer celery, fall back to local. Non-negotiable: the app must work with zero
infrastructure.

## 10. Routes — WS-API (owned), mounted by WS-MAIN

All routes live in `backend/api/*.py` as `router = APIRouter(...)`.
WS-MAIN includes them in `main.py` with `app.include_router(...)`.

Prefixes (must match the frontend contract exactly):

| module | prefix | endpoints |
|---|---|---|
| `investigate.py` | `/api` | `POST /investigate`, `POST /investigate-sync`, `GET /investigate/{inv_id}`, `GET /investigations`, `POST /photo-search`, `POST /name-search`, `POST /search`, `POST /compare` |
| `cases.py` | `/api/cases` | `GET /`, `POST /`, `GET /{case_id}`, `PATCH /{case_id}`, `DELETE /{case_id}`, `GET /{case_id}/stats`, `GET /{case_id}/entities`, `GET /{case_id}/timeline`, `POST /{case_id}/export` |
| `agents.py` | `/api` | `GET /agents`, `GET /agents/{name}`, `POST /agents/{name}/run`, `GET /agents/registry/graph` |
| `reports.py` | `/api` | `GET /investigate/{inv_id}/report?format=markdown\|pdf\|json`, `GET /investigate/{inv_id}/export` |
| `ops.py` | `/api` | `GET /health`, `GET /health/deep`, `GET /opsec/status`, `POST /opsec/newcircuit`, `GET /metrics`, `GET /config`, `WS /ws/pipeline/{inv_id}` |

**Backwards compatibility is mandatory** — these existing paths must keep
working unchanged (the frontend and README depend on them):
`POST /api/investigate`, `GET /api/investigate/{inv_id}`,
`GET /api/investigations`, `POST /api/search`, `POST /api/photo-search`,
`POST /api/name-search`, `GET /api/opsec/status`,
`POST /api/opsec/newcircuit`, `GET /api/health`, `WS /ws/pipeline/{inv_id}`.

Response shape for `GET /api/investigate/{inv_id}` must remain
`{inv_id, case_id, input, input_type, status, steps, agents, report, ...}`.

## 11. Frontend — WS-FE-* (owned)

Canonical app is `frontend/` (Next 16 App Router, React 19, Tailwind 4). Root
`pages/index.tsx` is legacy and gets deleted by WS-MAIN.

```ts
// frontend/lib/types.ts
export type EntityType = "person"|"email"|"username"|"domain"|"url"|"ip"
  |"phone"|"account"|"image"|"face"|"location"|"wallet"|"org"|"crypto"
  |"document"|"note"|"other";
export interface Entity { id,label,type,value?,confidence?,source?,first_seen?,last_seen?,attrs? }
export interface Signal { type,value,source,confidence,ts?,url?,attrs? }
export interface GraphNode { id,label,type,weight?,confidence?,group? }
export interface GraphEdge { source,target,label?,type?,weight?,confidence? }
export interface TimelineEvent { id,ts?,date,label,description?,source?,confidence?,entities? }
export interface AgentResult { agent,role?,icon?,status,output?,confidence?,reasoning?,entities_found?,signals?,latency_s?,tokens_used?,error?,steps? }
export interface Report { summary,input_type?,agents_activated?,entities?,signals?,graph?,timeline?,markdown?,confidence?,latency_s? }
export interface Investigation { inv_id,case_id,input,input_type,status,steps?,agents?,report?,started_at?,ended_at?,error? }
export interface Case { case_id,name,target?,created_at?,updated_at?,investigation_count?,tags?,notes? }

// frontend/lib/api.ts
export const API_BASE: string;                        // env or ""
export async function apiFetch<T>(path, init?): Promise<T>;
export function wsUrl(invId: string): string;        // ws:// or wss://, correct host
export async function submitInvestigation(input, opts?): Promise<Investigation>;
export function useInvestigationStream(invId, onEvent): { close(): void };
```

Rules:
- API base resolves to **same-origin `/api`** by default (frontend proxies to
  the backend) — `NEXT_PUBLIC_API_URL` overrides. Never hardcode a host.
- WebSocket URL must be derived from `window.location` + the resolved base,
  with automatic reconnect + backoff.
- Every fetch: typed, abortable (`AbortController`), and surfaces backend
  errors instead of silently swallowing.
- Accessibility: all interactive elements keyboard-reachable, labelled,
  visible focus ring, `prefers-reduced-motion` respected.
- Tailwind 4 (no `tailwind.config.js` needed — CSS-first `@theme`). Keep the
  existing dark Palantir/Maltego aesthetic and CSS variables in
  `globals.css`; extend, don't replace.

## 12. Definition of done (every workstream)

1. `python -c "import ..."` clean for every file you touched; no syntax errors.
2. No new import at module scope that can fail at import time (lazy-import heavy
   deps inside functions, matching the existing codebase style).
3. No `print()` in new code — use `observability.get_logger(__name__)`.
4. No secret values in logs, no secret values in source, no `eval`/`exec`.
5. Every new function has a docstring; every new module a header.
6. Public functions documented in the docstring with `Args:`/`Returns:`.
7. Errors: catch narrowly, return a structured `{"error": ...}` where the
   existing connector contract requires it; never crash a pipeline.
8. If you add a dependency, add it to `backend/requirements.txt` (or
   `frontend/package.json`) **and** make it optional-with-graceful-fallback if
   the existing code degrades gracefully without it.
