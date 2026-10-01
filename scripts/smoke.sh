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
#   ./scripts/smoke.sh --base-url http://127.0.0.1:8766/api/health
#                                      # test an already-running instance
#                                      # (this is what `make test-smoke` passes)
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
EXTERNAL=""

# `--base-url` is the contract `make test-smoke` already uses. The value is the
# *health* URL; derive the server root from it. An explicit instance is tested
# in place and never started, killed or reconfigured by this script.
while [ $# -gt 0 ]; do
  case "$1" in
    --base-url)
      HEALTH_URL="${2:-}"
      [ -n "$HEALTH_URL" ] || die "--base-url needs a value"
      BASE="${HEALTH_URL%/api/health}"
      BASE="${BASE%/}"
      EXTERNAL="1"
      shift 2
      ;;
    --base-url=*)
      HEALTH_URL="${1#--base-url=}"
      BASE="${HEALTH_URL%/api/health}"
      BASE="${BASE%/}"
      EXTERNAL="1"
      shift
      ;;
    -h|--help)
      sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

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

# Run the shared assertion suite against a base URL.
assert_suite() {
  "$PY" "$REPO_ROOT/scripts/smoke_assertions.py" "$1"
}

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

if [ -n "$EXTERNAL" ]; then
  # Testing an instance somebody else started: do not touch its environment,
  # do not manage its lifecycle, only assert against it.
  info "testing the running instance at $BASE (not started by this script)"
  assert_suite "$BASE" || exit 1
  ok "smoke suite passed against $BASE"
  printf '\n%ssmoke: PASS%s\n' "$BOLD$GREEN" "$RESET"
  exit 0
fi

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

# The suite deliberately exercises the guard rails — five rejected submissions
# plus a real investigation — which exceeds the production `investigate` budget
# of 6 rpm / burst 2. That limit is correct for a live deployment and wrong for
# a smoke run, so it is lifted here and the fact is stated in the output.
# The ceiling itself is asserted by backend/tests/test_smoke.py.
SMOKE_ENV=("RATE_LIMIT_SCOPE_RPM.investigate=600" "RATE_LIMIT_BURST.investigate=600")
info "lifted the investigate rate limit for this run (suite asserts the guard rails)"

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
  exec env "${SMOKE_ENV[@]}" "$PY" -m uvicorn main:app \
    --host "$HOST" --port "$PORT" --log-level warning
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

# The assertions live in scripts/smoke_assertions.py so the self-started and
# --base-url paths assert identically.
if ! assert_suite "$BASE"; then
  die "smoke assertions failed"
fi

ok "smoke suite passed"
printf '\n%ssmoke: PASS%s\n' "$BOLD$GREEN" "$RESET"