# Signal-OS Runbook

Day-two operations. Each procedure is written to be followed while something is
broken.

- [Triage: where to look first](#triage-where-to-look-first)
- [The store reports degraded mode](#the-store-reports-degraded-mode)
- [Debug a failed investigation](#debug-a-failed-investigation)
- [Rotate the Tor circuit](#rotate-the-tor-circuit)
- [GDPR erasure for a case](#gdpr-erasure-for-a-case)
- [Back up and restore the store](#back-up-and-restore-the-store)
- [Scale Celery workers](#scale-celery-workers)
- [Respond to an abuse report](#respond-to-an-abuse-report)
- [CI hermeticity](#ci-hermeticity)
- [Lint debt](#lint-debt)
- [Escalation](#escalation)

---

## Triage: where to look first

```bash
# 1. Is it up at all?
curl -s localhost:8766/api/health | jq '{status, store, cache, llm}'

# 2. Which dependency is unhappy? (concurrent probes with per-check timeouts)
curl -s localhost:8766/api/health/deep | jq '.checks'

# 3. What is configured but missing? (booleans only, never secret values)
curl -s localhost:8766/api/config | jq '.env_presence | to_entries | map(select(.value)) | .[]'

# 4. Recent errors
docker compose logs --tail=200 backend | grep -i error
journalctl -u signal-os -p err -n 200        # systemd
```

`status` on `/api/health` is `ok`, `degraded` or `down`:

| Status | Meaning | Act |
|---|---|---|
| `ok` | All dependencies reachable | — |
| `degraded` | At least one fell back to in-process | Read on; see the section below |
| `down` | The app cannot serve | Restart; check the startup log |

---

## The store reports degraded mode

### What you are seeing

```
[ERROR] signal-os.store: postgres start failed (OperationalError) — degrading to memory store
[ERROR] signal-os.store: store degraded to memory: postgres unavailable: OperationalError
[WARNING] signal-os.store: store running degraded — investigations are not persisted
```

and in the health payload:

```json
"store": {"backend": "memory", "degraded": true, "investigations": 0, "cases": 0}
```

### Is this an emergency?

Usually **no**. The platform is built to run this way. Every agent has a
deterministic offline path, so investigations still complete and reports are
still produced — see the measured p50 of 2.75 s in `docs/ARCHITECTURE.md` §4,
which was measured with no store at all.

**It becomes an emergency when you actually need persistence:**

- Cases and investigations vanish when the process restarts.
- `GET /api/investigate/{id}` returns 404 for anything submitted before a restart.
- GDPR erasure cannot be demonstrated — the records that must be deleted are not
  durable.
- Multi-replica deployments break outright: replicas with independent memory
  stores do not share state.

### Diagnose

```bash
# What is it actually trying?
curl -s localhost:8766/api/config | jq '{STORE_BACKEND, DATABASE_URL}'
grep -E '^(STORE_BACKEND|DATABASE_URL|SQLITE_PATH)=' .env

# Can it reach the database at all?
pg_isready -h localhost -p 5432 -U signal
psql "$DATABASE_URL" -c 'SELECT 1;'

# Docker
docker compose ps postgres
docker compose logs --tail=50 postgres
```

### Common causes, in order

1. **`DATABASE_URL` points at nothing** — the default in `.env.example` is a
   placeholder (`postgresql://signal:***@localhost:5432/signal_os`).
2. **Postgres is not running**, or is still starting. The first 10–20 s of
   startup legitimately logs connection refusals.
3. **Credentials wrong** — the `***` placeholder is literal.
4. **The network cannot reach it** — `docker compose up -d` was run on the host
   while the app expects the Compose network.
5. **`STORE_MAX_ROWS` too low** — this evicts rows, it does not cause degraded
   mode, but it looks like data loss.

### Fix

```bash
# Point at a real database
export STORE_BACKEND=postgres
export DATABASE_URL='postgresql://signal:<real-password>@localhost:5432/signal_os'

# or accept file persistence instead
export STORE_BACKEND=sqlite
export SQLITE_PATH=/var/lib/signal-os/signal_os.db

sudo systemctl restart signal-os
curl -s localhost:8766/api/health | jq .store.backend   # expect: "postgres"
```

To run with no database at all, set `STORE_BACKEND=memory` **explicitly** so
the degradation is intentional and visible rather than a silent failure.

### Cache in the same state

```
[WARNING] signal-os.cache: redis unavailable (ConnectionError) — using in-process cache
```

Harmless for correctness — the in-process TTL-LRU has identical semantics — but
the cache is per-process, so hit rate drops across replicas. Set
`REDIS_REQUIRED=true` only where losing the cache should be a hard failure.

---

## Debug a failed investigation

### Step 1 — get the record

```bash
curl -s -H "Authorization: Bearer $SIGNAL_API_KEY" \
  "localhost:8766/api/investigate/$INV_ID" | jq
```

```bash
jq '{status, error, latency_s, routing_reasoning, routing_confidence}' <<<"$REC"
jq -r '.agents[] | "\(.agent)\t\(.status)\t\(.latency_s)\t\(.error // "-")"' <<<"$REC"
```

### Step 2 — read the per-agent outcome

This is the table that answers "what happened":

| Field | Meaning |
|---|---|
| `status` | `done` / `error` / `partial` |
| `latency_s` | Wall time for that agent |
| `error` | The failure message, if any |
| `output` | The agent's actual payload |
| `steps` | Its own step log, same text the WebSocket streamed |

### Step 3 — classify

| Symptom | Likely cause | Fix |
|---|---|---|
| `status: "error"`, `error` mentions `timeout` | Agent exceeded `config.TIMEOUT_*`, or APEX's 180 s deadline | Raise the specific `TIMEOUT_<AGENT>`; check for a hung connector |
| `status: "error"` on fetch agents only | No network, or SSRF guard blocked the target | See [SSRF block](#ssrf-guard-blocked-the-target) |
| `status: "done"` but `output` contains `error` | Connector degraded — missing API key or dependency | `connector_health()`; set the key |
| `"error": "no provider configured"` | `LLM_ENABLED=true` with no provider key | Set a key, or `LLM_ENABLED=false` to accept deterministic output |
| `output` empty, `signals: []` | Input did not match any `InputType` | Pass `input_type` explicitly |
| `routing_confidence` low | Router unsure; consulted the LLM | Set `input_type` explicitly |
| Report markdown empty | QUILL found nothing to write | Check upstream agents first |

### Step 4 — inspect a connector

The registry reports readiness without touching the network:

```bash
cd backend && .venv/bin/python -c "
import json, connectors
h = connectors.connector_health()
for name, r in h['connectors'].items():
    if not r['installed']:
        print(f\"{name}: {r['notes']}\")
        print(f\"   missing: {r['missing']}\")
"
```

```bash
# Which connectors did this run actually touch?
jq -r '.agents[] | select(.output != null) | .agent' <<<"$REC"
```

### Step 5 — reproduce in isolation

```bash
# One agent, no pipeline
curl -X POST -H "Authorization: Bearer $SIGNAL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"input":"example.com","input_type":"domain"}' \
  "localhost:8766/api/agents/SCOUT/run" | jq
```

### Step 6 — watch it live

```bash
websocat "ws://localhost:8766/ws/pipeline/$INV_ID"
```

Subscribe **before** submitting for a run still in flight. A finished run replays
one `done` frame with the whole report.

### Step 7 — the log

```bash
LOG_LEVEL=DEBUG docker compose restart backend
docker compose logs -f backend | grep "$INV_ID"
```

> **Observed bug, not yet fixed.** `GET /api/investigate/{id}/export?format=markdown`
> sets `Content-Length` from `len(body)` — the **character** count — while the
> body is UTF-8 bytes. A report containing any non-ASCII character (an em dash, a
> name with an accent) declares a length ~34 bytes short and every strict client
> aborts the transfer. `api/reports.py:450`. Reproduce:
> `curl -sS -D - -o /dev/null 'localhost:8766/api/investigate/<id>/export?format=markdown'`
> → `content-length` smaller than the actual byte size. `?format=pdf`,
> `?format=json` and `?bundle=true` are unaffected because those bodies are ASCII
> or binary.

### SSRF guard blocked the target

```
[WARNING] security: blocked http://10.0.0.5/ — private range
```

Correct behaviour. The target resolved to a private address. Legitimate cases:

```bash
# An internal asset you are authorised to assess, inside a network you control
export ALLOW_PRIVATE_NETWORK=true
```

Read [`SECURITY.md` §3](SECURITY.md#3-ssrf-defence) first. Never set it on a host
that can reach cloud metadata.

---

## Rotate the Tor circuit

Rotates the exit node so the current one stops being attributable to you.

### Confirm routing is live

```bash
curl -s localhost:8766/api/opsec/status | jq
```

```json
{"opsec_enabled": true, "proxy": "socks5h://127.0.0.1:9050",
 "tor_connected": true, "current_ip": "<exit-ip>", "requests_routed": 128}
```

`opsec_enabled: false` → `OPSEC_ENABLED=true` and restart.
`tor_connected: false` → the Tor daemon is not reachable; fix that first.

### Rotate

```bash
curl -X POST -H "Authorization: Bearer $SIGNAL_API_KEY" \
  localhost:8766/api/opsec/newcircuit | jq
```

```json
{"success": true, "response": "250 OK"}
```

This sends `SIGNAL NEWNYM` to `TOR_CONTROL`. Tor treats a new circuit as *best
effort* — it may reuse the same exit node if that is the only viable path. The
endpoint reports what Tor said, not a guarantee.

Verify the exit actually changed:

```bash
curl -s localhost:8766/api/opsec/status | jq -r .current_ip   # before
curl -sX POST localhost:8766/api/opsec/newcircuit >/dev/null
sleep 5
curl -s localhost:8766/api/opsec/status | jq -r .current_ip   # after
```

### Rotate from the shell

```bash
printf 'AUTHENTICATE "%s"\r\nSIGNAL NEWNYM\r\nQUIT\r\n' "$TOR_CONTROL_PASSWORD" \
  | nc 127.0.0.1 9051
# 250 OK
```

### Rotation failed

| Response | Cause |
|---|---|
| `{"success": false, "error": "..."}` | Control port unreachable or auth failed |
| `250` but the IP is unchanged | Normal — Tor reused the exit. Retry, or `SIGNAL NEWNYM` twice. |
| Empty `TOR_CONTROL_PASSWORD` and auth fails | `HashedControlPassword` is set but `AUTHENTICATE ""` was sent |

### When to rotate

- After any investigation into a **new** sensitive subject.
- When a target might log your exit IP and you want to break the link.
- After a suspected correlation: the same exit IP hitting several unrelated
  third-party sources in a short window.

### Cautions

- `ControlPort` must stay on **loopback**. Anyone who can reach it can rotate
  your host's egress identity.
- An investigation already in flight keeps using the circuit it started on. The
  rotation affects subsequent requests.
- `requests_routed` counts requests through the proxy — it is not a
  privacy-preserving counter. Do not treat it as one.

---

## GDPR erasure for a case

Full threat-model context in
[`SECURITY.md` §9](SECURITY.md#9-gdpr-and-data-retention). Read that first — in
particular the four places deletion does **not** reach.

### Pre-flight

1. Confirm the scope of the request: which case(s), which subject.
2. Confirm the lawful basis for deleting (a consent withdrawal, an erasure
   request, a retention limit).
3. Note whether the subject appears in **other** cases — erasure is per case,
   not per person.

```bash
CASE_ID=<case_id>
API=http://localhost:8766
AUTH="Authorization: Bearer $SIGNAL_API_KEY"

# What is about to go?
curl -s -H "$AUTH" "$API/api/cases/$CASE_ID" | jq
curl -s -H "$AUTH" "$API/api/cases/$CASE_ID/stats" | jq
```

### 1. Delete the case and its investigations

```bash
curl -s -X DELETE -H "$AUTH" "$API/api/cases/$CASE_ID" | jq
# {"deleted": true, "case_id": "6a110ec7"}
```

This cascades: the case **and** every investigation filed under it are removed
in one transaction. Verify:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "$AUTH" "$API/api/cases/$CASE_ID"
# expect: 404
```

### 2. Confirm the audit record

```bash
curl -s -H "$AUTH" "$API/api/health/deep" | jq .store
```

A `case.deleted` audit event was written. That event is what makes the erasure
demonstrable — do not purge the audit trail along with the data.

### 3. Purge what the API does not reach

This is the step people skip, and skipping it means the erasure is incomplete.

```bash
# Qdrant semantic memory — VAULT stores entities here.
curl -s "${QDRANT_URL:-http://localhost:6333}/collections/${QDRANT_COLLECTION:-signal_os_memory}" | jq

# If the collection is dedicated to this subject, drop it. If it is shared,
# delete only the points for this case — inspect first; this is destructive.
curl -s -X DELETE \
  "${QDRANT_URL:-http://localhost:6333}/collections/${QDRANT_COLLECTION:-signal_os_memory}" | jq

# On-disk agent stores.
ls -la "${VAULT_DIR:-backend/vault_store}"
ls -la "${SENTINEL_DIR:-backend/sentinel_store}"
ls -la "${REPORTS_DIR:-backend/reports}"
# Remove artefacts belonging to this case or subject.

# Uploaded media — per-case temp dirs, deleted when the pipeline finishes.
find /tmp -maxdepth 2 -name '*signal*' -o -name '*upload*' 2>/dev/null
```

### 4. Backups

```bash
# Backups taken before the erasure still contain the rows. Erasure propagates
# forward, not backward. Apply your retention policy to the backup set.
pg_dump -Fc "$DATABASE_URL" > /tmp/post-erase-check.dump   # verify, do not archive
ls -la ~/backups/signal-os/
```

### 5. Record the response

Document: the request, the lawful basis, the case ids, what was deleted, what
was manually purged (Qdrant, agent stores, backups), and the date. Keep this
record even after the data is gone — that is the point of the exercise.

---

## Back up and restore the store

### Postgres

```bash
# Backup — custom format, compressed, restorable with pg_restore
pg_dump -Fc "$DATABASE_URL" -f "signal-os-$(date +%Y%m%d-%H%M%S).dump"

# Verify the dump is readable BEFORE relying on it
pg_restore --list signal-os-20261001-1200.dump | head

# Restore into an empty database (pg_restore will not overwrite a live one)
createdb signal_os_restore
pg_restore -d signal_os_restore signal-os-20261001-1200.dump

# Then swap DATABASE_URL and restart
```

### SQLite

```bash
# Never `cp` a live SQLite file — the WAL may hold newer data.
sqlite3 "$SQLITE_PATH" ".backup 'signal-os-$(date +%Y%m%d-%H%M%S).sqlite3'"
# or
sqlite3 "$SQLITE_PATH" "VACUUM INTO 'backup.sqlite3';"
```

### Agent stores and Qdrant

```bash
tar czf signal-os-stores-$(date +%Y%m%d).tar.gz \
  backend/vault_store backend/sentinel_store backend/reports

# Qdrant: create a snapshot, then download it
curl -s -X POST "${QDRANT_URL:-http://localhost:6333}/collections/${QDRANT_COLLECTION:-signal_os_memory}/snapshots" | jq
curl -s "${QDRANT_URL:-http://localhost:6333}/collections/${QDRANT_COLLECTION:-signal_os_memory}/snapshots" | jq
```

### A backup that has never been restored is not a backup

Quarterly, restore into a scratch environment and run the suite against it:

```bash
createdb signal_os_drill
pg_restore -d signal_os_drill latest.dump
DATABASE_URL=postgresql:///signal_os_drill ./scripts/smoke.sh --base-url "$API/api/health"
```

### Restore checklist

1. Stop the app (a restore into a live database is not supported).
2. Restore the database.
3. Restore the agent store directories and the Qdrant collection.
4. Restart and confirm `store.backend` is the persistent backend, not `memory`.
5. Run `./scripts/smoke.sh`.

---

## Scale Celery workers

### Check current capacity

```bash
docker compose logs celery-worker | tail -20
docker compose exec celery-worker celery -A celery_app.celery_app inspect active
docker compose exec celery-worker celery -A celery_app.celery_app inspect registered
```

```bash
# Queue depth via the broker
redis-cli -u "$REDIS_URL" LLEN celery
```

### Add workers

```bash
# Compose
docker compose up -d --scale celery-worker=4

# systemd / bare metal
celery -A celery_app.celery_app worker --loglevel=info --concurrency=4 \
       --max-tasks-per-child=200
```

### Choosing concurrency

Each worker process holds an investigation: ~10 agents and dozens of outbound
calls, so the bound is **memory and outbound concurrency**, not CPU.

| Setting | Use |
|---|---|
| `--concurrency=4` | 4 GB host. The default in `docker-compose.yml`. |
| `--concurrency=2` | 2 GB host, or heavy LLM work in-process |
| `--max-tasks-per-child=200` | Recycle periodically; agents cache models and leak |

`MAX_CONCURRENT_INVESTIGATIONS` (default 4) is the application-level semaphore —
**raise it alongside `--concurrency`**, or workers will sit idle behind it.

### Verify

```bash
celery -A celery_app.celery_app inspect ping -d celery@$(hostname)
curl -s localhost:8766/api/health/deep | jq '.checks'
```

### Caveats

- **Rate-limit buckets are per-process.** More workers mean a higher effective
  ceiling on `investigate` (6 rpm each). Move the limiter to Redis-backed shared
  counting before treating it as a blast-radius control.
- **Workers must not share an in-memory store.** Each needs the same
  `DATABASE_URL`/`SQLITE_PATH`. SQLite with concurrent writers corrupts — use
  Postgres for multi-worker.
- `docker-compose.yml` sets `stop_grace_period` above
  `celery_app.task_time_limit` (600 s) so a long task is not killed mid-flight.

---

## Respond to an abuse report

Assume the report may come from someone who was actually investigated.

### 1. Preserve first

```bash
# Stop further collection against the complaining subject immediately.
# The id is in the audit trail.
docker compose logs --since=24h backend | grep -i "$COMPLAINANT"
```

```bash
# Identify every case touching the subject
curl -s -H "$AUTH" "$API/api/investigations?limit=200" | jq \
  '.[] | select(.input | ascii_downcase | contains("'"$SUBJECT"'"))'
```

### 2. Erase

Follow [GDPR erasure](#gdpr-erasure-for-a-case) in full — including the manual
purges. Do not skip step 3 because the API's 200 is enough; it is not.

### 3. Review your own conduct

Answer honestly, in writing:

- What lawful basis did you assert?
- Was the investigation in scope?
- Which inputs and third-party sources were used?
- Was OPSEC on? If not, the subject's operator can associate the traffic with
  your egress IP.

### 4. Check for a systemic failure

An abuse report is often a symptom. Look for:

- **Auth disabled in production** — `curl -s $API/api/config | jq .env_presence`,
  and confirm `ENVIRONMENT` is not `development`.
- **Rate limits absent** — if dozens of runs are possible, the limiter is not
  armed or is per-process across many workers.
- **Tor off** — if `opsec_enabled: false`, the source IP was exposed.
- **Scope keys over-issued** — if one key has `*`, every holder can investigate
  anyone.

### 5. Respond

Acknowledge the complaint. State what was found, what was deleted, and what is
being changed to prevent recurrence. If the platform was misused by an operator,
say so plainly rather than implying the subject was mistaken.

### 6. Report up

Escalate to whoever is accountable for the deployment. This is not a support
ticket; it is a potential data-protection incident and may carry a notification
deadline.

---

## CI hermeticity

CI must never reach the network or depend on a service that infra CI does not
start. The backend suite enforces this in `tests/conftest.py`:

| Dependency | CI substitution | Why |
|---|---|---|
| `OPSEC_ENABLED` | `false` | A Tor-routed request hangs — the opposite of a fast test |
| `DATABASE_URL` | SQLite in `tmp_path` | Never the placeholder DSN; exercises the real SQL path |
| `SEARXNG_URL` | a closed local port | Fails in ms against a refused connection |
| `REDIS_URL` | a closed local port | Forces the in-process cache path |
| `CELERY_ENABLED` | `false` | Tasks run in-process; no broker needed |
| `VAULT_DIR`/`SENTINEL_DIR`/`REPORTS_DIR` | temp dirs | Tests never write into the working tree |
| outbound sockets | blocked | A regression that reaches the network fails loudly instead of hitting a third party |

```bash
cd backend && OPSEC_ENABLED=false .venv/bin/python -m pytest tests/ -q
cd backend && OPSEC_ENABLED=false .venv/bin/python -m pytest tests/test_smoke.py -q
./scripts/test-smoke.sh            # shell smoke, CI-shaped
RUN_PYTEST=1 ./scripts/test-smoke.sh   # + pytest
```

If a test needs the network, it does not belong in this suite.

---

## Lint debt

`make lint` is **ratcheted** against `infra/lint-baseline.json` via
`scripts/lint_ratchet.py`: CI fails only when the finding count *increases*.
That keeps inherited debt from blocking feature work while stopping it growing.

```bash
make lint-strict     # the full, unratcheted finding list
make lint-baseline   # accept the current count as the new budget
make lint-fix        # apply safe autofixes
make fmt             # rewrite with ruff format
```

Rules live in `infra/ruff.toml`, referenced explicitly by CI, `make` and
pre-commit so they cannot drift. The ignore list is documented rule by rule:
each entry is either a style opinion that would churn files other workstreams
own, or a rule that contradicts a documented project convention.

`ruff-format` drift outside the entrypoints is deliberately non-blocking for
the same reason. Reformatting ~43 files owned by other workstreams is not a
side effect of a lint fix.

---

## Escalation

| Symptom | Severity | Action |
|---|---|---|
| Auth disabled in production | **P0** | Set `AUTH_DISABLED=false`, restart, audit the trail for misuse |
| SSRF reaching cloud metadata | **P0** | Set `ALLOW_PRIVATE_NETWORK=false`, rotate the instance role credentials |
| Leaked API key in git history | **P0** | Revoke at the provider first, then clean history. Deleting a line is not a rotation. |
| Tor silently off during a sensitive run | **P1** | Stop the run, note the exposed egress IP, rotate before continuing |
| Store degraded in production | **P1** | Data is not durable; fix before relying on it |
| Investigation returns `queued` forever | **P1** | Serverless — see [`DEPLOY.md` §5](DEPLOY.md#5-vercel) |
| Abuse report from an investigated subject | **P1** | [Abuse procedure](#respond-to-an-abuse-report) |
| Report export truncates | **P2** | Known bug at `api/reports.py:450`; use `?format=json` |
| Connector returns `{"error": ...}` | **P3** | Expected without a key or dependency; `connector_health()` |

---

## Related documents

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — how it is built
- [`API.md`](API.md) — every endpoint
- [`DEPLOY.md`](DEPLOY.md) — deployment topologies
- [`SECURITY.md`](SECURITY.md) — threat model and gaps
- [`CONTRACTS.md`](CONTRACTS.md) — frozen interface contracts