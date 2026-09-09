.PHONY: help test lint type-check format format-check security semgrep check migrate dev docker-up docker-down docker-logs worker beat css coverage clean schema-skill mcp-health pg-observe-setup build-hardened license-gen license-validate

DB_CONTAINER ?= dotmac_erp_db
DB_NAME ?= dotmac_erp

# The roots ruff formats. `format` (writes) and `format-check` (verifies) must
# read the SAME list, or the gate can pass over a root the writer never
# touched.
#
# That list is the WHOLE REPOSITORY, and it has to be, because CI's pre-commit
# job is the widest formatter gate this repo has: `pre-commit/action` runs
# `--all-files`, and the ruff-format hook declares no `files:` and no
# `exclude:`, so it format-checks every tracked .py — tests/, scripts/,
# alembic/, tools/, .claude/hooks/ and gunicorn.conf.py included. While this
# read `app/`, `make check` was a strictly weaker question than CI's, and a
# tree could pass every local gate and still be rejected on push. It was: eight
# blocks under tests/ collapsed to <= 88 columns and ruff rejoined them.
#
# `.` rather than an enumeration, so a new top-level Python directory is
# covered the day it is added instead of the day someone remembers this line.
# ruff honours .gitignore and its own default excludes, so `.` does not mean
# .venv or node_modules. If .seabone/ ever carries Python, it needs a
# [tool.ruff] extend-exclude entry to match pre-commit's `exclude:` — the
# exclusion belongs in ruff's own config, not in a second root list here.
FORMAT_ROOTS ?= .

# The roots ruff LINTS, same argument and the same defect: pre-commit's
# `- id: ruff` hook is equally unscoped, so CI already lints every tracked .py
# while `make lint` asked about app/ alone. pyproject's per-file-ignores for
# tests/, alembic/, scripts/, tools/ and .claude/hooks/ exist precisely because
# ruff reaches them.
LINT_ROOTS ?= .

# Default target
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Quality ──────────────────────────────────────────────

lint: ## Run ruff linter
	poetry run ruff check $(LINT_ROOTS)

format: ## Format code with ruff
	poetry run ruff format $(FORMAT_ROOTS)
	poetry run ruff check --fix $(LINT_ROOTS)

format-check: ## Verify formatting without writing (same check CI runs)
	poetry run ruff format --check $(FORMAT_ROOTS)

type-check: ## Run mypy type checker
	poetry run mypy app/ --ignore-missing-imports

security: ## Run bandit security scan
	poetry run bandit -r app/ -c pyproject.toml -q

semgrep: ## Run semgrep custom rules (DotMac anti-patterns)
	poetry run pre-commit run semgrep --all-files

check: lint format-check type-check security semgrep ## Run all quality checks (lint + format + type-check + security + semgrep)

# ─── Testing ──────────────────────────────────────────────

test: ## Run test suite
	poetry run pytest tests/ -q

test-v: ## Run test suite (verbose)
	poetry run pytest tests/ -v

test-cov: ## Run tests with coverage report
	poetry run pytest tests/ --cov=app --cov-report=term-missing

test-fast: ## Run tests, stop on first failure
	poetry run pytest tests/ -x --tb=short

test-e2e: ## Run end-to-end browser tests
	poetry run pytest tests/e2e/ -v --headed

# ─── Database ─────────────────────────────────────────────

migrate: ## Apply all pending migrations + regenerate schema skill
	poetry run alembic upgrade head
	@echo "Regenerating schema skill..."
	@poetry run python scripts/generate_schema_skill.py

migrate-new: ## Create a new migration (usage: make migrate-new msg="add users table")
	poetry run alembic revision --autogenerate -m "$(msg)"

migrate-down: ## Rollback last migration
	poetry run alembic downgrade -1

migrate-history: ## Show migration history
	poetry run alembic history --verbose

# ─── Claude Code ─────────────────────────────────────────────

schema-skill: ## Regenerate database schema skill for Claude Code
	poetry run python scripts/generate_schema_skill.py

mcp-health: ## Validate MCP DB config and read-only connectivity
	poetry run python scripts/check_mcp_db.py

pg-observe-setup: ## Enable pg_stat_statements and grant monitoring permissions (run docker compose up -d db first if new)
	@test -n "$$PG_OBSERVER_PASSWORD" || { \
	  echo "PG_OBSERVER_PASSWORD is not set. The observability role's password is"; \
	  echo "deliberately not stored in the repository -- load it from OpenBao."; \
	  exit 1; }
	docker exec -i $(DB_CONTAINER) psql -U postgres -d $(DB_NAME) \
	  -v observer_password="$$PG_OBSERVER_PASSWORD" < scripts/setup_pg_observability.sql

# ─── Development ──────────────────────────────────────────

dev: ## Run dev server with hot reload
	python -m uvicorn app.main:app --reload --port 8000

worker: ## Run Celery worker
	celery -A app.celery_app worker --loglevel=info

beat: ## Run Celery beat scheduler
	celery -A app.celery_app beat --loglevel=info

css: ## Build Tailwind CSS
	npm run build:css

css-watch: ## Watch and rebuild Tailwind CSS
	npm run watch:css

# ─── Docker ───────────────────────────────────────────────

docker-up: ## Start all Docker containers
	docker compose up -d

docker-down: ## Stop all Docker containers
	docker compose down

docker-logs: ## Tail Docker container logs
	docker compose logs -f --tail=100

docker-rebuild: ## Rebuild and restart app container
	docker compose build app && docker compose up -d app

docker-shell: ## Open shell in app container
	docker exec -it dotmac_erp_app bash

docker-migrate: ## Run migrations inside Docker + regenerate schema skill
	@test -n "$$MIGRATION_DATABASE_URL" || { \
	  echo "MIGRATION_DATABASE_URL must connect as app_admin"; \
	  exit 2; }
	docker compose run --rm -e MIGRATION_DATABASE_URL app \
	  alembic upgrade heads
	@echo "Regenerating schema skill..."
	@poetry run python scripts/generate_schema_skill.py

# ─── Licensing ───────────────────────────────────────────

build-hardened: ## Build hardened Docker image (Nuitka-compiled)
	docker build -f Dockerfile.hardened -t dotmac-erp-hardened:local .

license-gen: ## Run license generation CLI (usage: make license-gen ARGS="generate-keypair --out-dir ./keys")
	poetry run python tools/license_gen.py $(ARGS)

license-validate: ## Validate a .lic file (usage: make license-validate LIC=dotmac.lic PUB=keys/public.pem)
	poetry run python tools/license_gen.py validate-license --lic $(LIC) --pub $(PUB)

# ─── Pre-commit ───────────────────────────────────────────

pre-commit-install: ## Install pre-commit hooks
	poetry run pre-commit install

pre-commit-run: ## Run pre-commit on all files
	poetry run pre-commit run --all-files

# ─── Cleanup ──────────────────────────────────────────────

clean: ## Remove build artifacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf htmlcov/ .coverage
