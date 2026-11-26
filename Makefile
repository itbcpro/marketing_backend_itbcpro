.PHONY: verify db-roles help setup up down migrate seed dev-api dev-worker dev-beat lint typecheck test layers check-i18n check-language openapi format

API := cd apps/api &&

help:
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-16s %s\n", $$1, $$2}'

setup: ## Install backend dependencies
	$(API) uv sync
	python tools/compile_messages.py

up: ## Start Postgres and Redis
	docker compose up -d --wait postgres redis

down: ## Stop local services
	docker compose down

migrate: ## Apply database migrations
	$(API) uv run python manage.py migrate

db-roles: ## Create the runtime database role used with row level security
	$(API) uv run python manage.py ensure_db_roles

verify: ## Every gate the CI runs
	$(MAKE) lint typecheck layers test check-i18n check-language

seed: ## Create demo workspace and users
	$(API) uv run python manage.py seed_dev

dev-api: ## Run the API dev server
	$(API) uv run python manage.py runserver 127.0.0.1:8001

dev-worker: ## Run background jobs (agents, assistant drafts, outbox); needed for anything asynchronous
	$(API) DJANGO_SETTINGS_MODULE=config.settings.dev uv run celery -A config worker --pool=threads --concurrency=4 -Q default,agents,reports,media,low,ingest.social,publish.social -l info

dev-beat: ## Run the scheduler (outbox dispatch, token refresh, health checks) in a second terminal
	$(API) DJANGO_SETTINGS_MODULE=config.settings.dev uv run celery -A config beat -l info

lint: ## Lint everything
	$(API) uv run ruff check . && uv run ruff format --check .
	python tools/check_language.py

typecheck: ## Type-check backend and frontend
	$(API) uv run mypy .

layers: ## Enforce architectural import contracts
	$(API) uv run lint-imports

test: ## Backend tests with coverage
	$(API) uv run pytest --cov

check-i18n: ## Validate translation catalogs
	$(API) uv run python ../../tools/check_i18n.py

check-language: ## Fail on non-English script outside catalogs
	python tools/check_language.py

openapi: ## Regenerate the OpenAPI schema (the front repo copies packages/api-client)
	$(API) uv run python ../../tools/export_openapi.py

format: ## Auto-format
	$(API) uv run ruff check . --fix && uv run ruff format .
