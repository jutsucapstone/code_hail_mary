# JUTSU — developer entry points (spec §6).
#
# Recipes stay shell-agnostic: this repo is developed on Windows (mingw32-make,
# cmd shell) and built in CI on Linux, so no bashisms, no `&&` chains, no
# subshells. Every line is a single portable command.
#
# Targets for slices that have not landed yet fail loudly and name the slice,
# rather than silently succeeding and letting a gate pass on nothing.

.PHONY: help dev api worker up down migrate migrate-down migrate-rev psql seed test preflight eval gate deploy clean

help:
	@echo "JUTSU targets"
	@echo "  make dev        Next.js dev server on :3210"
	@echo "  make api        FastAPI gateway on :8000"
	@echo "  make worker     arq worker"
	@echo "  make up         start Postgres + Neo4j + Redis (needs Docker)"
	@echo "  make down       stop them"
	@echo "  make migrate    apply Postgres migrations (Neo4j at S2)"
	@echo "  make migrate-down  roll Postgres back to base"
	@echo "  make psql       psql shell into the dev database"
	@echo "  make seed       ingest the pilot corpus                [S3]"
	@echo "  make test       full test suite"
	@echo "  make preflight  lint + typecheck + tests  (required before commit, §4.15)"
	@echo "  make eval       evaluation harness                     [S9]"
	@echo "  make gate       phase gate harness                     [S9]"

# ---------------------------------------------------------------- run

dev:
	pnpm --filter web dev

api:
	uv run --package api uvicorn jutsu_api.main:app --reload --port 8000

worker:
	uv run --package worker arq jutsu_worker.main.WorkerSettings

up:
	docker compose -f infra/docker/compose.yml up -d

down:
	docker compose -f infra/docker/compose.yml down

# ---------------------------------------------------------------- quality

lint-web:
	pnpm --filter web lint

typecheck-web:
	pnpm --filter web typecheck

lint-py:
	uv run ruff check .

format-check-py:
	uv run ruff format --check .

typecheck-py:
	uv run mypy packages apps

test-py:
	uv run pytest -q

test-hooks:
	node scripts/hooks/preflight-on-commit.test.mjs

test: test-hooks test-py

# §4.15 — this is what the commit hook runs. Keep it fast enough to run every time.
preflight: lint-web typecheck-web test-hooks lint-py format-check-py typecheck-py test-py
	@echo "preflight OK"

# ---------------------------------------------------------------- schema

migrate:
	uv run --package jutsu-db alembic -c packages/db/alembic.ini upgrade head

migrate-down:
	uv run --package jutsu-db alembic -c packages/db/alembic.ini downgrade base

migrate-rev:
	uv run --package jutsu-db alembic -c packages/db/alembic.ini revision --autogenerate -m "$(m)"

psql:
	docker compose -f infra/docker/compose.yml exec postgres psql -U jutsu -d jutsu

# ---------------------------------------------------------------- not yet implemented

seed:
	@echo "seed is implemented in S3. See docs/plan-phase-1.md"
	@exit 1

eval:
	@echo "eval is implemented in S9. See docs/plan-phase-1.md"
	@exit 1

gate:
	@echo "gate is implemented in S9. See docs/plan-phase-1.md"
	@exit 1

deploy:
	@echo "deploy is implemented in S29. See docs/jutsu-master-spec.md"
	@exit 1

clean:
	pnpm --filter web exec rm -rf .next
