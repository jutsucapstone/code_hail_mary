# JUTSU — developer entry points (spec §6).
#
# Recipes stay shell-agnostic: this repo is developed on Windows (mingw32-make,
# cmd shell) and built in CI on Linux, so no bashisms, no `&&` chains, no
# subshells. Every line is a single portable command.
#
# Targets for slices that have not landed yet fail loudly and name the slice,
# rather than silently succeeding and letting a gate pass on nothing.
#
# Anything needing configuration goes through `uv run --env-file .env`, which is one
# portable command and therefore allowed by the rule above — `set -a; . ./.env` is not.
# Without it `make migrate` failed on a correctly configured checkout while printing
# "Copy .env.example to .env", advice for a file that was already there.
#
# Package names are the ones in pyproject.toml: `jutsu-api`, not `api`. The short forms
# were wrong here from the start, so `make api` and `make worker` had never run.

.PHONY: help dev api worker up down migrate migrate-down migrate-rev psql seed test preflight eval gate deploy clean api-types api-types-check

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
	uv run --env-file .env --package jutsu-api uvicorn jutsu_api.main:app --reload --port 8000

worker:
	uv run --env-file .env --package jutsu-worker arq jutsu_worker.main.WorkerSettings

up:
	docker compose -f infra/docker/compose.yml up -d

down:
	docker compose -f infra/docker/compose.yml down

# ---------------------------------------------------------------- quality

lint-web:
	pnpm --filter web lint

typecheck-web:
	pnpm --filter web typecheck

# --no-cache, and it is not paranoia. Ruff caches per file, keyed on that file content
# and the config — so a change elsewhere that alters how an import is *classified* leaves
# the cached verdict in place. Adding a root conftest.py did exactly that: `make
# preflight` reported clean while CI, which is always a fresh checkout, failed on an
# unsorted import in a file nobody had touched. A gate that can answer from a stale cache
# is a gate that can be green about code it did not look at. The suite is 57 files; the
# cache was buying milliseconds.
lint-py:
	uv run ruff check --no-cache .

format-check-py:
	uv run ruff format --check .

# Checked one tree per pass, not all at once. Every test directory holds a conftest.py,
# and mypy derives module names from the nearest directory without an __init__.py — so a
# single invocation sees several modules called "conftest", calls it ambiguous, and
# refuses to check anything at all. Splitting the runs keeps every file checked without
# scattering __init__.py through a src-layout tree.
#
# The root conftest.py needs its own line: it sits outside both trees, and it is the
# file that loads .env for the test suite, so an error in it silently disables 112 tests.
#
# Only ONE conftest.py may exist under apps/ — a second one collides and mypy checks
# nothing. Per-app fixtures therefore live in their test module, not beside it.
typecheck-py:
	uv run mypy packages
	uv run mypy apps
	uv run mypy conftest.py

test-py:
	uv run pytest -q

test-hooks:
	node scripts/hooks/preflight-on-commit.test.mjs

test: test-hooks test-py

# §4.13 — frontend types are generated from Pydantic via OpenAPI, never hand-written.
# Derived from the app object rather than a running server, so the check is a pure
# function of the source and cannot flake on whether the API happened to be up.
api-types:
	uv run python scripts/emit-openapi.py > apps/web/lib/openapi.json
	pnpm exec openapi-typescript apps/web/lib/openapi.json -o apps/web/lib/api-schema.d.ts

# Fail if regenerating would produce something different from what is checked in. A
# stale client is worse than no client: it type-checks against a contract the server no
# longer honours.
#
# Compared against the files ON DISK, not against HEAD. Diffing against HEAD would fail
# for any legitimate API change and make it impossible to ever commit one — the check
# would be asking "has the API changed", when the question is "is the client in step".
# git diff --no-index is used because it ships with git and behaves the same on every
# platform, unlike diff or fc.
api-types-check:
	uv run python scripts/emit-openapi.py > apps/web/lib/.openapi.check.json
	pnpm exec openapi-typescript apps/web/lib/.openapi.check.json -o apps/web/lib/.api-schema.check.d.ts
	git --no-pager diff --no-index --exit-code apps/web/lib/openapi.json apps/web/lib/.openapi.check.json
	git --no-pager diff --no-index --exit-code apps/web/lib/api-schema.d.ts apps/web/lib/.api-schema.check.d.ts
	node -e "for (const f of process.argv.slice(1)) require('fs').rmSync(f, { force: true })" apps/web/lib/.openapi.check.json apps/web/lib/.api-schema.check.d.ts

# §4.15 — this is what the commit hook runs. Keep it fast enough to run every time.
preflight: lint-web typecheck-web test-hooks lint-py format-check-py typecheck-py test-py api-types-check
	@echo "preflight OK"

# ---------------------------------------------------------------- schema

migrate:
	uv run --env-file .env --package jutsu-db alembic -c packages/db/alembic.ini upgrade head

migrate-down:
	uv run --env-file .env --package jutsu-db alembic -c packages/db/alembic.ini downgrade base

migrate-rev:
	uv run --env-file .env --package jutsu-db alembic -c packages/db/alembic.ini revision --autogenerate -m "$(m)"

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
