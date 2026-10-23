.PHONY: dev test test-slow test-e2e frontend-check lint build clean

dev:
	docker compose -f docker-compose.dev.yml up --build

# Backend core lane: what CI runs on every pull request.
test:
	cd backend && python -m pytest -m "not slow and not live" -n auto

# Docker and storage-provider integration tests.
test-slow:
	cd backend && python -m pytest -m "slow and not live" -n auto

test-unit:
	cd backend && python -m pytest tests/unit/ -n auto

test-integration:
	cd backend && python -m pytest tests/integration/ -n auto

# Playwright end-to-end suite (runs in the official Playwright image).
test-e2e:
	frontend/scripts/e2e.sh --docker

frontend-check:
	cd frontend && npm run check && npx vitest run && npm run build

lint:
	cd backend && ruff check app/ && ruff format --check app/ && bandit -q -r app -ll

build:
	docker build -t islamdiaa/arkive:local .

frontend-dev:
	cd frontend && npm run dev

frontend-build:
	cd frontend && npm run build

clean:
	docker compose -f docker-compose.dev.yml down -v
