.PHONY: dev test lint build push clean

dev:
	docker compose -f docker-compose.dev.yml up --build

test:
	cd backend && python -m pytest tests/ -v

test-unit:
	cd backend && python -m pytest tests/unit/ -v --tb=short

test-integration:
	cd backend && python -m pytest tests/integration/ -v --tb=short

lint:
	cd backend && ruff check app/ && ruff format --check app/

build:
	docker build -t ghcr.io/islamdiaa/arkive:latest .

push: build
	docker push ghcr.io/islamdiaa/arkive:latest

frontend-dev:
	cd frontend && npm run dev

frontend-build:
	cd frontend && npm run build

clean:
	docker compose -f docker-compose.dev.yml down -v
