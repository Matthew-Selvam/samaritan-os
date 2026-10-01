#!/usr/bin/env bash
# dev-setup.sh — one-shot bootstrap for a Signal-OS development machine.
#
# Creates the Python venv, installs backend + frontend dependencies, installs
# the pre-commit hooks, and generates a .env from .env.example if one is missing.
#
# Safe to re-run: every step is idempotent.
#
#   ./scripts/dev-setup.sh              # full bootstrap
#   ./scripts/dev-setup.sh --no-hooks   # skip the pre-commit install
#   ./scripts/dev-setup.sh --check      # report status, change nothing
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"
VENV_DIR="$BACKEND_DIR/.venv"

INSTALL_HOOKS=1
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --no-hooks) INSTALL_HOOKS=0 ;;
    --check)    CHECK_ONLY=1 ;;
    -h|--help)  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# ── Output helpers ───────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
  RED=$'\033[31m'; BLUE=$'\033[34m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; RESET=""
fi

info()  { printf '%s==>%s %s\n' "$BLUE" "$RESET" "$*"; }
ok()    { printf '%s  ok%s %s\n' "$GREEN" "$RESET" "$*"; }
warn()  { printf '%s  ! %s%s\n' "$YELLOW" "$*" "$RESET"; }
die()   { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# ── Preflight ────────────────────────────────────────────────────────────────
need() {
  command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not on PATH. $2"
}

[ "$CHECK_ONLY" -eq 1 ] || {
  need python3 "Install Python 3.11+ from https://python.org"
  need node    "Install Node 20+ from https://nodejs.org"
  need npm     "Comes with Node."
}

python_ok() {
  python3 - <<'PY' 2>/dev/null
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

if [ "$CHECK_ONLY" -eq 0 ] && ! python_ok; then
  die "Python 3.11+ required, found $(python3 -V 2>&1). The repo targets 3.11 (CI uses 3.11)."
fi

node_major="$(node -v 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/' || echo 0)"
if [ "$CHECK_ONLY" -eq 0 ] && [ "${node_major:-0}" -lt 20 ]; then
  die "Node 20+ required, found $(node -v 2>/dev/null || echo 'nothing')."
fi

# ── 1. Python venv ───────────────────────────────────────────────────────────
if [ "$CHECK_ONLY" -eq 1 ]; then
  if [ -x "$VENV_DIR/bin/python" ]; then
    ok "venv present: $($VENV_DIR/bin/python -V)"
  else
    warn "venv missing: $VENV_DIR"
  fi
else
  info "Python virtualenv"
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -m venv "$VENV_DIR"
    ok "created $VENV_DIR"
  else
    ok "reusing $VENV_DIR"
  fi

  "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip setuptools wheel
  ok "pip upgraded"
fi

# ── 2. Backend dependencies ──────────────────────────────────────────────────
if [ "$CHECK_ONLY" -eq 1 ]; then
  if "$VENV_DIR/bin/python" -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
    ok "backend deps importable"
  else
    warn "backend deps incomplete"
  fi
else
  info "Backend dependencies"
  # requirements.txt is the single source of truth. It pins `httpx[socks]`,
  # without which every OPSEC-routed request raises ImportError.
  "$VENV_DIR/bin/python" -m pip install --quiet -r "$BACKEND_DIR/requirements.txt"
  ok "installed from backend/requirements.txt"
fi

# ── 3. Frontend dependencies ─────────────────────────────────────────────────
if [ "$CHECK_ONLY" -eq 1 ]; then
  if [ -d "$FRONTEND_DIR/node_modules/next" ]; then
    ok "frontend node_modules present"
  else
    warn "frontend node_modules missing"
  fi
else
  info "Frontend dependencies"
  # npm ci (not npm install) so the lockfile is authoritative — same as CI.
  if [ -f "$FRONTEND_DIR/package-lock.json" ]; then
    ( cd "$FRONTEND_DIR" && npm ci --no-audit --no-fund --loglevel=error )
  else
    warn "no package-lock.json; falling back to npm install"
    ( cd "$FRONTEND_DIR" && npm install --no-audit --no-fund --loglevel=error )
  fi
  ok "installed frontend/node_modules"
fi

# ── 4. .env ──────────────────────────────────────────────────────────────────
if [ "$CHECK_ONLY" -eq 1 ]; then
  if [ -f "$REPO_ROOT/.env" ]; then
    ok ".env present"
  else
    warn ".env missing — copy .env.example and set POSTGRES_PASSWORD / NEO4J_PASSWORD"
  fi
elif [ -f "$REPO_ROOT/.env" ]; then
  ok ".env already exists (left untouched)"
else
  cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
  # Generate throwaway passwords rather than shipping the example defaults into
  # a real .env that someone forgets to edit.
  if command -v openssl >/dev/null 2>&1; then
    gen() { openssl rand -hex "$1"; }
  else
    gen() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
  fi
  # macOS sed needs an explicit backup suffix; GNU sed does not accept one.
  if sed --version >/dev/null 2>&1; then
    sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(gen 24)|" "$REPO_ROOT/.env"
    sed -i "s|^NEO4J_PASSWORD=.*|NEO4J_PASSWORD=$(gen 24)|" "$REPO_ROOT/.env"
    sed -i "s|^MINIO_SECRET_KEY=.*|MINIO_SECRET_KEY=$(gen 24)|" "$REPO_ROOT/.env"
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(gen 32)|" "$REPO_ROOT/.env"
  else
    sed -i '' "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(gen 24)|" "$REPO_ROOT/.env"
    sed -i '' "s|^NEO4J_PASSWORD=.*|NEO4J_PASSWORD=$(gen 24)|" "$REPO_ROOT/.env"
    sed -i '' "s|^MINIO_SECRET_KEY=.*|MINIO_SECRET_KEY=$(gen 24)|" "$REPO_ROOT/.env"
    sed -i '' "s|^SECRET_KEY=.*|SECRET_KEY=$(gen 32)|" "$REPO_ROOT/.env"
  fi
  ok "created .env with generated secrets (review the API keys before use)"
fi

# ── 5. Pre-commit hooks ──────────────────────────────────────────────────────
if [ "$CHECK_ONLY" -eq 1 ]; then
  if [ -f "$REPO_ROOT/.git/hooks/pre-commit" ]; then
    ok "pre-commit hook installed"
  else
    warn "pre-commit hook not installed"
  fi
elif [ "$INSTALL_HOOKS" -eq 1 ]; then
  info "Pre-commit hooks"
  if ! command -v pre-commit >/dev/null 2>&1; then
    # Install into the venv so we do not mutate the user's global site-packages.
    "$VENV_DIR/bin/python" -m pip install --quiet pre-commit
    PRECOMMIT="$VENV_DIR/bin/pre-commit"
  else
    PRECOMMIT="$(command -v pre-commit)"
  fi
  ( cd "$REPO_ROOT" && "$PRECOMMIT" install --install-hooks )
  ok "pre-commit hooks installed"
else
  info "Skipping pre-commit (--no-hooks)"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo ""
  echo "  Environment status"
  echo ""
else
  echo ""
  echo "  Setup complete. Next:"
  echo ""
  echo "    make check     # everything CI runs"
  echo "    make up        # the full Docker stack"
  echo "    make dev       # or run the two dev servers locally"
  echo ""
  echo "  Docs: docs/DEPLOY.md · docs/ARCHITECTURE.md · docs/RUNBOOK.md"
  echo ""
fi
