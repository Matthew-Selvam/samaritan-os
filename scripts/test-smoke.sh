#!/usr/bin/env bash
# test-smoke.sh — the CI entry point for the smoke assertions.
#
# Same guarantees as smoke.sh, but shaped for a CI runner:
#
#   * non-interactive, no colour, no TTY assumptions
#   * hard 300s ceiling so a hung pipeline cannot burn a runner for an hour
#   * exits non-zero on the first failure, with the failing assertion named
#   * optionally runs the pytest version too (RUN_PYTEST=1) so CI gates on the
#     same checks the suite enforces
#
#   ./scripts/test-smoke.sh              # shell smoke suite
#   RUN_PYTEST=1 ./scripts/test-smoke.sh # + pytest tests/test_smoke.py
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO_ROOT/backend/.venv/bin/python"

[ -x "$PY" ] || { echo "ERR no interpreter at $PY — run ./scripts/dev-setup.sh" >&2; exit 1; }

# CI never has Tor, Postgres, Redis, Qdrant or an LLM key. Say so up front so a
# degraded-store result is not mistaken for a failure.
echo "==> environment: hermetic (no infra, no LLM, no Tor)"

status=0

echo
echo "==> 1/2 scripts/smoke.sh"
if NO_COLOR=1 SMOKE_PORT="${CI_SMOKE_PORT:-8768}" bash "$REPO_ROOT/scripts/smoke.sh"; then
  echo "  ok smoke.sh passed"
else
  echo "  ERR smoke.sh failed" >&2
  status=1
fi

if [ "${RUN_PYTEST:-0}" = "1" ]; then
  echo
  echo "==> 2/2 pytest tests/test_smoke.py"
  if (
    cd "$REPO_ROOT/backend" && \
    OPSEC_ENABLED=false LLM_ENABLED=false AUDIT_LOG=false \
    "$PY" -m pytest tests/test_smoke.py -q -p no:warnings
  ); then
    echo "  ok test_smoke.py passed"
  else
    echo "  ERR test_smoke.py failed" >&2
    status=1
  fi
else
  echo
  echo "==> 2/2 pytest skipped (set RUN_PYTEST=1 to include it)"
fi

echo
if [ "$status" -eq 0 ]; then
  echo "smoke: PASS"
else
  echo "smoke: FAIL" >&2
fi
exit "$status"