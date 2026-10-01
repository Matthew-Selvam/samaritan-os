#!/usr/bin/env bash
# smoke.sh — end-to-end smoke test with zero infrastructure.
#
# Starts the backend on a scratch port, waits for /api/health, runs a REAL
# investigation through the whole 16-agent pipeline, asserts the report shape,
# and tears the server down. No Postgres, no Redis, no Qdrant, no Neo4j, no MinIO
# and no Tor are required: the store/cache layers degrade to in-process
# fallbacks, the LLM tier is disabled, and the connectors degrade to their
# deterministic offline paths.
#
#   ./scripts/smoke.sh                 # run it
#   SMOKE_PORT=9001 ./scripts/smoke.sh # pick a port
#   SMOKE_KEEP=1 ./scripts/smoke.sh    # leave the server up for poking
#
# Exits 0 only if every assertion passes.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
VENV_DIR="$BACKEND_DIR/.venv"
PY="$VENV_DIR/bin/python"

PORT="${SMOKE_PORT:-8766}"
HOST="127.0.0.1"
BASE="http://$HOST:$PORT"
LOG="$(mktemp "${TMPDIR:-/tmp}/signal-os-smoke.XXXXXX.log")"
SERVER_PID=""

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
  RED=$'\033[31m'; BLUE=$'\033[34m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; RESET=""
fi

info() { printf '%s==>%s %s\n' "$BLUE" "$RESET" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s  !%s %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '%sERR%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# ── Cleanup ─────────────────────────────────────────────────────────────────
cleanup() {
  local rc=$?
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    if [ "${SMOKE_KEEP:-0}" = "1" ]; then
      warn "leaving server up on $BASE (pid $SERVER_PID); log: $LOG"
    else
      kill "$SERVER_PID" 2>/dev/null || true
      # uvicorn --reload spawns a child; take the whole group down.
      pkill -P "$SERVER_PID" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$SERVER_PID" 2>/dev/null || break
        sleep 0.2
      done
      kill -9 "$SERVER_PID" 2>/dev/null || true
    fi
  fi
  if [ "$rc" -ne 0 ] && [ "${SMOKE_KEEP:-0}" != "1" ] && [ -s "$LOG" ]; then
    printf '\n%s─── last 40 log lines ───%s\n' "$BOLD" "$RESET" >&2
    tail -n 40 "$LOG" >&2 || true
    printf '%s──────────────────────────%s\n' "$BOLD" "$RESET" >&2
  fi
  [ "${SMOKE_KEEP:-0}" = "1" ] || rm -f "$LOG"
  exit "$rc"
}
trap cleanup EXIT INT TERM

# ── Preflight ───────────────────────────────────────────────────────────────
[ -x "$PY" ] || die "no interpreter at $PY — run ./scripts/dev-setup.sh first"
info "interpreter $("$PY" -V 2>&1)"

# Hermetic by construction: no auth key to forget, no Tor to hang on, no
# external LLM to call. These are the same flags CI uses.
export ENVIRONMENT="${ENVIRONMENT:-development}"
export AUTH_DISABLED="${AUTH_DISABLED:-true}"
export OPSEC_ENABLED="${OPSEC_ENABLED:-false}"
export LLM_ENABLED="${LLM_ENABLED:-false}"
export CELERY_ENABLED="${CELERY_ENABLED:-false}"
export REDIS_REQUIRED="${REDIS_REQUIRED:-false}"
export AUDIT_LOG="${AUDIT_LOG:-false}"
export DEBUG="${DEBUG:-true}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export STORE_MAX_ROWS="${STORE_MAX_ROWS:-200}"

# Refuse to run against an existing server on this port — we would be measuring
# somebody else's instance and then killing it.
if "$PY" - "$HOST" "$PORT" <<'PYEOF' 2>/dev/null
import socket, sys
s = socket.socket()
s.settimeout(0.4)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PYEOF
then
  die "port $PORT is already in use — set SMOKE_PORT to something free"
fi

# ── Boot ────────────────────────────────────────────────────────────────────
info "starting backend on $BASE (log: $LOG)"
(
  cd "$BACKEND_DIR" || exit 1
  exec "$PY" -m uvicorn main:app --host "$HOST" --port "$PORT" --log-level warning
) >>"$LOG" 2>&1 &
SERVER_PID=$!

ready=0
for _ in $(seq 1 100); do          # 100 x 0.3s = 30s ceiling
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    die "server exited during startup (see $LOG)"
  fi
  if "$PY" - "$BASE" <<'PYEOF' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + "/api/health", timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
  then
    ready=1
    break
  fi
  sleep 0.3
done
[ "$ready" -eq 1 ] || die "backend never became healthy within 30s (see $LOG)"
ok "GET /api/health is 200"

# Everything below runs inside one Python block so the assertions share state.
"$PY" - "$BASE" <<'PYEOF'
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1]
FAIL = []
CHECKS = 0


def check(label, condition, detail=""):
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok {label}", flush=True)
    else:
        print(f"  ERR {label}: {detail}", flush=True)
        FAIL.append(label)


def call(method, path, payload=None, expect=200):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}")
        except Exception:
            return e.code, {"raw": body.decode(errors="replace")[:400]}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def jget(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


print("=== ops surface", flush=True)
status, health = call("GET", "/api/health")
check("health 200", status == 200, f"got {status}")
# "degraded" is the *correct* answer with zero infrastructure running: the
# store fell back to memory. What must never happen is "down".
check("health status is ok or degraded",
      health.get("status") in ("ok", "degraded"), str(health.get("status")))
check("health names the platform", health.get("platform") == "signal-os", str(health)[:200])

status, deep = call("GET", "/api/health/deep")
check("health/deep 200", status == 200, f"got {status}")
check("health/deep reports a status", bool(deep.get("status")), str(deep)[:200])

status, metrics = call("GET", "/api/metrics")
check("metrics 200", status == 200, f"got {status}")

status, cfg = call("GET", "/api/config")
check("config 200", status == 200, f"got {status}")

print("=== agents", flush=True)
status, agents = call("GET", "/api/agents")
names = agents.get("items") if isinstance(agents, dict) else None
names = [a.get("name") for a in names] if isinstance(names, list) else None
check("agents 200", status == 200, f"got {status}")
check("16 agents registered", bool(names) and len(names) == 16,
      f"got {len(names) if names else 0}: {names}")
check("APEX is present", bool(names) and "APEX" in names, str(names)[:200])
check("QUILL is present", bool(names) and "QUILL" in names, str(names)[:200])

print("=== the real pipeline", flush=True)
t0 = time.perf_counter()
status, queued = call(
    "POST", "/api/investigate",
    {"input": "example.com", "input_type": "domain"},
)
elapsed = time.perf_counter() - t0
check("investigate 200", status == 200, f"got {status}: {str(queued)[:300]}")
inv_id = queued.get("inv_id")
case_id = queued.get("case_id")
check("investigate returns inv_id", bool(inv_id), str(queued)[:300])
check("investigate returns case_id", bool(case_id), str(queued)[:300])
ok(f"queued {inv_id} in {elapsed:.2f}s")

# The async path is fire-and-forget; poll the record like a real client would.
rec = {}
deadline = time.time() + 120
while time.time() < deadline:
    status, rec = call("GET", f"/api/investigate/{inv_id}")
    if status == 200 and rec.get("status") in ("done", "completed", "error", "failed"):
        break
    time.sleep(0.5)

check("investigation reached a terminal state",
      rec.get("status") in ("done", "completed", "error", "failed"),
      f"status={rec.get('status')!r} after {120 - int(deadline - time.time())}s")
check("investigation did not error",
      rec.get("status") in ("done", "completed"),
      f"error={rec.get('error')!r}")

report = rec.get("report")
check("report is present", bool(report), "report was empty/None")
check("report is non-empty", bool(report) and len(str(report)) > 80,
      f"report length={len(str(report)) if report else 0}")

ran = rec.get("agents") or []
check("pipeline recorded agent results", len(ran) >= 9,
      f"only {len(ran)} agents recorded: {[a.get('name') for a in ran]}")
check("APEX ran", any(a.get("name") == "APEX" for a in ran),
      str([a.get("name") for a in ran]))
check("QUILL ran", any(a.get("name") == "QUILL" for a in ran),
      str([a.get("name") for a in ran]))
degraded = [a.get("name") for a in ran if a.get("status") not in ("done", "ok", "completed", None)]
check("no agent hard-failed", not degraded, f"degraded: {degraded}")

print("=== reports", flush=True)
for fmt in ("markdown", "json"):
    status, _ = call("GET", f"/api/investigate/{inv_id}/report?format={fmt}")
    check(f"report?format={fmt} 200", status == 200, f"got {status}")

print("=== listing", flush=True)
status, listing = call("GET", "/api/investigations?limit=5")
check("investigations 200", status == 200, f"got {status}")
check("investigations is a list", isinstance(listing, list), type(listing).__name__)
check("our investigation is listed",
      isinstance(listing, list) and any(r.get("inv_id") == inv_id for r in listing),
      "inv_id absent from the listing")

print("=== cases", flush=True)
status, created = call("POST", "/api/cases", {"name": f"smoke-{int(time.time())}"})
check("case create 200", status == 200, f"got {status}: {str(created)[:200]}")
new_case = created.get("case") if isinstance(created, dict) else None
cid = (new_case or {}).get("case_id") or created.get("case_id")
check("case create returns case_id", bool(cid), str(created)[:300])

if cid:
    status, got = call("GET", f"/api/cases/{cid}")
    check("case get 200", status == 200, f"got {status}")
    status, patched = call("PATCH", f"/api/cases/{cid}", {"notes": "smoke test"})
    check("case patch 200", status == 200, f"got {status}")
    status, stats = call("GET", f"/api/cases/{cid}/stats")
    check("case stats 200", status == 200, f"got {status}")
    status, _ = call("DELETE", f"/api/cases/{cid}")
    check("case delete 200", status == 200, f"got {status}")

print("=== negative cases (the guard rails)", flush=True)
status, _ = call("POST", "/api/investigate", {"input": ""})
check("empty input rejected", status in (400, 422), f"got {status}")

status, _ = call("POST", "/api/investigate", {"input": "x", "bogus_field": 1})
check("unknown field rejected (extra=forbid)", status in (400, 422), f"got {status}")

status, _ = call("GET", "/api/investigate/..%2F..%2Fetc%2Fpasswd")
check("path traversal id rejected", status in (400, 404, 422), f"got {status}")

status, _ = call("GET", "/api/agents/NOT_AN_AGENT")
check("unknown agent 404", status == 404, f"got {status}")

print()
if FAIL:
    print(f"FAILED {len(FAIL)}/{CHECKS} checks:", file=sys.stderr)
    for name in FAIL:
        print(f"  - {name}", file=sys.stderr)
    sys.exit(1)
print(f"PASSED all {CHECKS} checks (inv_id={inv_id}, {elapsed:.2f}s to queue)", flush=True)
PYEOF

ok "smoke suite passed"
printf '\n%ssmoke: PASS%s\n' "$BOLD$GREEN" "$RESET"