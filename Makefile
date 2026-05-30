.PHONY: up down logs backend frontend dev install

# ── Docker ────────────────────────────────────────────────────────────────────

up:
	docker-compose up -d

down:
	docker-compose down

logs:
	docker-compose logs -f

# ── Local dev (no Docker) ─────────────────────────────────────────────────────

backend:
	cd backend && uvicorn main:app --reload --port 8766

frontend:
	cd frontend && npm run dev

dev:
	@command -v concurrently >/dev/null 2>&1 && \
		concurrently \
			--names "backend,frontend" \
			--prefix-colors "cyan,magenta" \
			"cd backend && uvicorn main:app --reload --port 8766" \
			"cd frontend && npm run dev" \
	|| ( \
		echo "concurrently not found — starting backend in background, frontend in foreground"; \
		cd backend && uvicorn main:app --reload --port 8766 & \
		cd frontend && npm run dev \
	)

install:
	pip install -r backend/requirements.txt
	cd frontend && npm install
