#!/usr/bin/env bash
# bench.sh — latency and throughput benchmark for the investigation pipeline.
#
# This substantiates the performance claims in the README: it measures a real
# POST /api/investigate round trip (queue time), the full pipeline wall clock
# (poll GET until terminal), and the resulting p50 / p95 latency and throughput.
#
# It runs with zero infrastructure: the store degrades to memory, the cache to
# in-process, the LLM tier is off, and the connectors degrade to their
# deterministic offline paths. That measures the *floor* — the platform with
# everything wired. Real deployments with LLM and live connectors are slower;
# treat these numbers as the lower bound, not the promise.
#
#   ./scripts/bench.sh                  # 5 runs (default)
#   BENCH_N=20 ./scripts/bench.sh       # more runs for a tighter p95
#   BENCH_PORT=9001 ./scripts/bench.sh  # different port
#   BENCH_WARM=0 ./scripts/bench.sh     # skip the warm-up run
#
# Exits 0 if every run completed, 1 otherwise.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
PY="$BACKEND_DIR/.venv/bin/python"

PORT="${BENCH_PORT:-8767}"
HOST="127.0.0.1"
N="${BENCH_N:-5}"
WARM="${BENCH_WARM:-1}"
LOG="$(mktemp "${TMPDIR:-/tmp}/signal-os-bench.XXXXXX.log")"
SERVER_PID=""

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
  RED=$'\033[31m'; BLUE=$'\033[34m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; RESET=""
fi

info() { printf '%s==>%s %s\n' "$BLUE" "$RESET" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$GREEN" "$RESET" "$*"; }
die()  { printf '%sERR%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

cleanup() {
  local rc=$?
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    pkill -P "$SERVER_PID" 2>/dev/null || true
    sleep 0.3
    kill -9 "$SERVER_PID" 2>/dev/null || true
  fi
  [ "$rc" -eq 0 ] || { printf '\n--- last 30 log lines ---\n' >&2; tail -n 30 "$LOG" >&2 || true; }
  rm -f "$LOG"
  exit "$rc"
}
trap cleanup EXIT INT TERM

[ -x "$PY" ] || die "no interpreter at $PY — run ./scripts/dev-setup.sh first"

# Same hermetic flags as the smoke test.
export ENVIRONMENT="${ENVIRONMENT:-development}"
export AUTH_DISABLED="${AUTH_DISABLED:-true}"
export OPSEC_ENABLED="${OPSEC_ENABLED:-false}"
export LLM_ENABLED="${LLM_ENABLED:-false}"
export CELERY_ENABLED="${CELERY_ENABLED:-false}"
export REDIS_REQUIRED="${REDIS_REQUIRED:-false}"
export AUDIT_LOG="${AUDIT_LOG:-false}"
export DEBUG="${DEBUG:-false}"
export LOG_LEVEL="${LOG_LEVEL:-ERROR}"
export STORE_MAX_ROWS="${STORE_MAX_ROWS:-5000}"

# The `investigate` scope is limited to 6 rpm / burst 2 by default, which is a
# deliberate blast-radius control, not a benchmark obstacle. A local benchmark
# would spend 10s per run waiting on a token bucket and report nonsense, so the
# ceiling is lifted here and reported in the output. Set BENCH_KEEP_LIMITS=1 to
# measure under the production limit instead (expect it to take ~10s/run).
if [ "${BENCH_KEEP_LIMITS:-0}" = "1" ]; then
  info "keeping the default rate limits (investigate = 6 rpm) — runs will be slow"
  BENCH_ENV=()
else
  # These names contain a dot, so they must be passed via `env`, not `export`.
  BENCH_ENV=("RATE_LIMIT_SCOPE_RPM.investigate=${BENCH_RPM:-600}"
             "RATE_LIMIT_SCOPE_RPM.read=${BENCH_RPM:-600}")
  info "lifted the investigate rate limit to ${BENCH_RPM:-600} rpm for benchmarking"
fi

if "$PY" - "$HOST" "$PORT" <<'PYEOF' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(0.4)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PYEOF
then
  die "port $PORT already in use — set BENCH_PORT to something free"
fi

info "starting backend on http://$HOST:$PORT"
(
  cd "$BACKEND_DIR" || exit 1
  exec env "${BENCH_ENV[@]}" "$PY" -m uvicorn main:app \
    --host "$HOST" --port "$PORT" --log-level warning
) >>"$LOG" 2>&1 &
SERVER_PID=$!

ready=0
for _ in $(seq 1 100); do
  kill -0 "$SERVER_PID" 2>/dev/null || die "server exited during startup (see $LOG)"
  if "$PY" - "http://$HOST:$PORT" <<'PYEOF' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + "/api/health", timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
  then ready=1; break; fi
  sleep 0.3
done
[ "$ready" -eq 1 ] || die "backend never became healthy (see $LOG)"
ok "healthy"

"$PY" - "http://$HOST:$PORT" "$N" "$WARM" <<'PYEOF'
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

BASE, N, WARM = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1"

# A domain, a username and a phone number exercise three different primary
# swarms, so the percentiles are not one code path measured N times.
INPUTS = [
    ("domain",    "example.com"),
    ("domain",    "example.org"),
    ("username",  "torproject"),
    ("domain",    "example.net"),
    ("username",  "github"),
]


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.loads(r.read())


def one(kind, value, poll_timeout=180):
    """Run one investigation end to end.

    Returns (queue_ms, total_ms, agent_count, confidence) or raises.
    """
    t0 = time.perf_counter()
    queued = post("/api/investigate", {"input": value, "input_type": kind})
    t_queue = time.perf_counter() - t0

    inv_id = queued["inv_id"]
    deadline = time.time() + poll_timeout
    rec = {}
    while time.time() < deadline:
        rec = get(f"/api/investigate/{inv_id}")
        if rec.get("status") in ("done", "error", "failed", "completed"):
            break
        time.sleep(0.05)
    t_total = time.perf_counter() - t0
    if rec.get("status") != "done":
        raise RuntimeError(f"investigation {inv_id} ended as {rec.get('status')!r}")
    return t_queue * 1000, t_total * 1000, len(rec.get("agents") or []), rec.get("confidence") or 0.0


def pct(values, p):
    """Nearest-rank percentile of a sorted copy of *values*."""
    if not values:
        return float("nan")
    s = sorted(values)
    k = max(1, min(len(s), int(round(p / 100 * len(s) + 0.5))))
    return s[k - 1]


if WARM:
    print("=== warm-up (not measured)", flush=True)
    one(*INPUTS[0])

print(f"\n{'=' * 62}\n=== {N} measured runs\n{'=' * 62}", flush=True)
queue_ms, total_ms, agent_counts, confs = [], [], [], []
agent_names = set()
for i in range(N):
    kind, value = INPUTS[i % len(INPUTS)]
    try:
        q, t, n, c = one(kind, value)
    except Exception as exc:
        print(f"  run {i + 1}/{N} FAILED: {type(exc).__name__}: {exc}", flush=True)
        continue
    queue_ms.append(q)
    total_ms.append(t)
    agent_counts.append(n)
    confs.append(c)
    print(f"  run {i + 1:>2}/{N}  {kind:<9} {value:<14} "
          f"queue {q:7.1f}ms  total {t:8.1f}ms  agents {n:>2}  conf {c:.0%}", flush=True)

if not total_ms:
    print("all runs failed — nothing to report", file=sys.stderr)
    sys.exit(1)

wall = sum(total_ms) / 1000
throughput = len(total_ms) / wall if wall else float("nan")

print(f"\n{'=' * 62}\n=== results (n={len(total_ms)})\n{'=' * 62}")
print(f"  queue latency   p50 {statistics.median(queue_ms):8.1f} ms   "
      f"min {min(queue_ms):8.1f} ms   max {max(queue_ms):8.1f} ms")
print(f"  pipeline p50    {pct(total_ms, 50):8.1f} ms")
print(f"  pipeline p95    {pct(total_ms, 95):8.1f} ms")
print(f"  pipeline min    {min(total_ms):8.1f} ms")
print(f"  pipeline max    {max(total_ms):8.1f} ms")
print(f"  mean agents     {statistics.mean(agent_counts):8.1f}")
print(f"  mean confidence {statistics.mean(confs):8.1%}")
print(f"  throughput      {throughput:8.2f} investigations/s (sequential)")
print(f"  {'=' * 62}")
print("\nNote: measured with LLM_ENABLED=false, no Tor, no Postgres/Redis/Qdrant.")
print("This is the deterministic floor. The LLM and live-connector paths add")
print("network latency on top; see docs/DEPLOY.md for capacity planning.")
sys.exit(0)
PYEOF

ok "benchmark complete"