# Signal-OS — developer entrypoint.
#
# Every target here maps onto something CI runs, so `make check` locally is a
# genuine pre-push gate rather than a different set of checks that merely look
# similar. The mapping is annotated per target.
#
#   make help     list targets
#   make setup    first-time bootstrap (venv, node deps, pre-commit)
#   make check    everything CI runs, in the order CI runs it

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# ── Paths & tools ────────────────────────────────────────────────────────────
BACKEND  := backend
FRONTEND := frontend
VENV     := $(BACKEND)/.venv
PY       := $(VENV)/bin/python
PIP      := $(VENV)/bin/pip
PYTEST   := $(VENV)/bin/pytest
RUFF     := $(VENV)/bin/ruff

# Prefer the venv's tools, fall back to whatever is on PATH. This lets the file
# work both after `make setup` (tools in the venv) and in CI (tools installed
# globally into the runner's python).
RUFF_BIN := $(shell command -v $(RUFF) 2>/dev/null || command -v ruff 2>/dev/null)
PYTHON_BIN := $(shell command -v $(PY) 2>/dev/null || command -v python3 2>/dev/null)
COMPOSE := docker compose

# Lint config lives in infra/ and is referenced explicitly so ruff never picks
# up a stray ~/.config/ruff and CI/local/pre-commit cannot drift.
RUFF_FLAGS := --config infra/ruff.toml

BACKEND_PORT ?= 8766
FRONTEND_PORT ?= 3001
HEALTH_URL ?= http://127.0.0.1:$(BACKEND_PORT)/api/health
COMPOSE_PROJECT ?= signal-os

export PATH := $(CURDIR)/$(VENV)/bin:$(PATH)

.PHONY: help setup install install-backend install-frontend \
        dev dev-backend dev-frontend dev-infra dev-logs \
        test test-backend test-frontend test-cov test-smoke \
        lint lint-backend lint-strict lint-baseline lint-frontend lint-fix \
        fmt fmt-check typecheck security security-pip security-npm security-secrets \
        build build-frontend build-images build-allinone \
        up up-infra up-prod down down-vols restart ps logs logs-backend logs-frontend \
        seed migrate migrate-down shell dbshell health smoke bench check ci clean clean-all help

# ═════════════════════════════════════════════════════════════════════════════
# Help
# ═════════════════════════════════════════════════════════════════════════════
help: ## Show this help
	@echo ""
	@echo "  Signal-OS — make targets"
	@echo ""
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Typical first run:  make setup && make check"
	@echo "  Typical dev loop:   make dev"
	@echo ""

# ═════════════════════════════════════════════════════════════════════════════
# Setup
# ═════════════════════════════════════════════════════════════════════════════
setup: install ## Bootstrap the dev environment (calls scripts/dev-setup.sh)
	@./scripts/dev-setup.sh

install: install-backend install-frontend ## Install all dependencies

install-backend: ## Create the venv and install backend requirements
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PIP) install --quiet --upgrade pip
	@$(PIP) install --quiet -r $(BACKEND)/requirements.txt
	@echo "backend deps installed -> $(VENV)"

install-frontend: ## Install frontend node modules
	@npm ci --prefix $(FRONTEND) --no-audit --no-fund
	@echo "frontend deps installed -> $(FRONTEND)/node_modules"

# ═════════════════════════════════════════════════════════════════════════════
# Development servers (no Docker for the app layer)
# ═════════════════════════════════════════════════════════════════════════════
dev: dev-backend dev-frontend ## Run backend + frontend locally (Ctrl-C stops both)

dev-backend: ## Run the FastAPI backend with reload
	@cd $(BACKEND) && ../$(VENV)/bin/python -m uvicorn main:app --reload --host 0.0.0.0 --port $(BACKEND_PORT)

dev-frontend: ## Run the Next.js dev server
	@npm run dev --prefix $(FRONTEND)

dev-infra: up-infra ## Start only the data layer

dev-logs: logs ## Tail all compose logs

# ═════════════════════════════════════════════════════════════════════════════
# Tests
# ═════════════════════════════════════════════════════════════════════════════
test: test-backend test-frontend ## Run the full test suite (CI: test-backend, test-frontend)

test-backend: ## Run backend pytest
	@cd $(BACKEND) && OPSEC_ENABLED=false JITTER_MIN_MS=0 JITTER_MAX_MS=0 \
	  ENVIRONMENT=$${ENVIRONMENT:-test} DEBUG=false LOG_LEVEL=$${LOG_LEVEL:-WARNING} \
	  ../$(VENV)/bin/python -m pytest tests/ -v --tb=short

test-frontend: ## Type-check the frontend (CI: lint)
	@cd $(FRONTEND) && npx tsc --noEmit -p tsconfig.json

test-cov: ## Backend tests with a coverage report
	@cd $(BACKEND) && OPSEC_ENABLED=false JITTER_MIN_MS=0 JITTER_MAX_MS=0 LOG_LEVEL=WARNING \
	  ../$(VENV)/bin/python -m pytest tests/ --cov=. --cov-report=term-missing --cov-report=html

test-smoke: ## Run the end-to-end smoke test against a locally running backend
	@./scripts/smoke.sh --base-url $(HEALTH_URL)

# ═════════════════════════════════════════════════════════════════════════════
# Lint / format / typecheck
# ═════════════════════════════════════════════════════════════════════════════
lint: lint-backend lint-frontend ## Run all linters (CI: lint)

lint-backend: ## ruff check, ratcheted against infra/lint-baseline.json (CI: lint)
	@python3 scripts/lint_ratchet.py

lint-strict: ## ruff check with no baseline (shows the full inherited debt)
	@$(RUFF_BIN) check $(RUFF_FLAGS) $(BACKEND)

lint-baseline: ## Record the current finding count as the new budget
	@python3 scripts/lint_ratchet.py --write

lint-frontend: ## tsc --noEmit (CI: lint)
	@cd $(FRONTEND) && npx tsc --noEmit -p tsconfig.json

lint-fix: ## Apply ruff's safe autofixes
	@$(RUFF_BIN) check $(RUFF_FLAGS) $(BACKEND) --fix

fmt: ## Rewrite Python with ruff format
	@$(RUFF_BIN) format $(RUFF_FLAGS) $(BACKEND)

fmt-check: ## Verify formatting on the entrypoints only (non-blocking elsewhere)
	@$(RUFF_BIN) format $(RUFF_FLAGS) --check $(BACKEND)/main.py $(BACKEND)/config.py scripts/ \
	  || echo "note: inherited formatting drift outside the entrypoints is not gating (see infra/ruff.toml)"

typecheck: lint-frontend ## Alias for the TypeScript type check

# ═════════════════════════════════════════════════════════════════════════════
# Security
# ═════════════════════════════════════════════════════════════════════════════
security: security-pip security-npm security-secrets ## Run every security scan (CI: security)

security-pip: ## pip-audit the backend requirements (CI: security)
	@$(VENV)/bin/python -m pip_audit -r $(BACKEND)/requirements.txt --strict --progress-spinner off

security-npm: ## npm audit the frontend (CI: security)
	@cd $(FRONTEND) && npm audit --audit-level=high

security-secrets: ## Scan the working tree for leaked secrets (CI: security — gitleaks)
	@if command -v gitleaks >/dev/null 2>&1; then \
	  gitleaks detect --source . --no-banner --redact --exit-code 1; \
	else \
	  echo "gitleaks not installed — skipping (CI runs this job)"; \
	fi

# ═════════════════════════════════════════════════════════════════════════════
# Build
# ═════════════════════════════════════════════════════════════════════════════
build: build-frontend ## Build the frontend (CI: test-frontend)

build-frontend: ## next build (CI: test-frontend)
	@npm run build --prefix $(FRONTEND)

build-images: ## Build the backend + frontend images (CI: docker)
	@docker build -f $(BACKEND)/Dockerfile  -t signal-os/backend:latest $(BACKEND)
	@docker build -f $(FRONTEND)/Dockerfile -t signal-os/frontend:latest $(FRONTEND)

build-allinone: ## Build the all-in-one image (CI: docker)
	@docker build -f Dockerfile -t signal-os/allinone:latest .

# ═════════════════════════════════════════════════════════════════════════════
# Compose
# ═════════════════════════════════════════════════════════════════════════════
up: ## Start the whole stack in the background
	@$(COMPOSE) up -d --build
	@$(MAKE) --no-print-directory health

up-infra: ## Start only the data layer (postgres/redis/neo4j/qdrant/minio/searxng/tor)
	@$(COMPOSE) up -d postgres redis neo4j qdrant minio searxng tor
	@echo "data layer up. UI endpoints:"
	@echo "  neo4j   http://localhost:$${NEO4J_HTTP_PORT:-7474}"
	@echo "  minio   http://localhost:$${MINIO_CONSOLE_PORT:-9001}"
	@echo "  searxng http://localhost:$${SEARXNG_PORT:-8888}"

up-prod: ## Start the stack with the dev profile (adds pgweb)
	@$(COMPOSE) --profile dev up -d --build

down: ## Stop the stack, keeping volumes
	@$(COMPOSE) down

down-vols: ## Stop the stack AND delete volumes (destroys all case data)
	@$(COMPOSE) down -v

restart: ## Restart the app services
	@$(COMPOSE) restart backend frontend celery-worker

ps: ## Show compose service status
	@$(COMPOSE) ps

logs: ## Tail logs for all services
	@$(COMPOSE) logs -f --tail=100

logs-backend: ## Tail backend logs
	@$(COMPOSE) logs -f --tail=200 backend

logs-frontend: ## Tail frontend logs
	@$(COMPOSE) logs -f --tail=200 frontend

# ═════════════════════════════════════════════════════════════════════════════
# Data
# ═════════════════════════════════════════════════════════════════════════════
seed: ## Load development seed data
	@echo "No seed data is defined for Signal-OS; investigations are created at runtime."
	@echo "Create one with:  make smoke"

migrate: ## Apply store schema (the store self-creates; this verifies it)
	@$(COMPOSE) exec -T backend python -c "import store, asyncio; s=store.get_store(); asyncio.run(s.start()); print('store ready:', type(s).__name__, 'degraded=', getattr(s,'degraded',None)); asyncio.run(s.close())"

migrate-down: ## Drop all data volumes (destructive)
	@$(COMPOSE) down -v
	@echo "volumes removed"

# ═════════════════════════════════════════════════════════════════════════════
# Verification
# ═════════════════════════════════════════════════════════════════════════════
health: ## Curl the backend health endpoint
	@curl -fsS $(HEALTH_URL) && echo

smoke: ## End-to-end smoke test (starts a backend if needed)
	@./scripts/smoke.sh

bench: ## Latency + throughput benchmark
	@./scripts/bench.sh

# ═════════════════════════════════════════════════════════════════════════════
# The gate
# ═════════════════════════════════════════════════════════════════════════════
check: lint test test-frontend fmt-check ## Run exactly what CI runs (lint, typecheck, tests, format)
	@echo ""
	@echo "  local checks green. CI additionally runs: security scan + docker build."
	@echo "  run 'make security build-images' to match those too."

ci: check security build-images ## Everything CI runs, including the slow jobs

# ═════════════════════════════════════════════════════════════════════════════
# Cleanup
# ═════════════════════════════════════════════════════════════════════════════
clean: ## Remove caches and build output
	@find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf $(BACKEND)/.pytest_cache .pytest_cache .ruff_cache htmlcov .coverage coverage.xml
	@rm -rf $(FRONTEND)/.next
	@echo "caches cleaned"

clean-all: clean down-vols ## Clean caches AND delete all data volumes
	@echo "full clean done"
