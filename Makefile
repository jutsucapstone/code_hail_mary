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

.PHONY: help dev api worker up down migrate migrate-pg migrate-graph migrate-down migrate-pg-down migrate-graph-down migrate-status migrate-rev psql seed test test-py-cov preflight eval gate deploy clean api-types api-types-check

help:
	@echo "JUTSU targets"
	@echo "  make dev        Next.js dev server on :3210"
	@echo "  make api        FastAPI gateway on :8000"
	@echo "  make worker     arq worker"
	@echo "  make up         start Postgres + Neo4j + Redis (needs Docker)"
	@echo "  make down       stop them"
	@echo "  make migrate    apply Postgres + Neo4j migrations"
	@echo "  make migrate-down  roll both back to base"
	@echo "  make migrate-status  what is applied, in both stores"
	@echo "  make psql       psql shell into the dev database"
	@echo "  make seed       ingest a local corpus   ORG=<uuid> ROOT=<dir> [EMBED=--embed] [SAMPLE=--sample] [LOG=<file>]"
	@echo "  make test       full test suite"
	@echo "  make test-py-cov  the suite with per-package coverage"
	@echo "  make preflight  lint + typecheck + tests  (required before commit, §4.15)"
	@echo "  make eval       phase 1 gate report      ORG=<uuid> [LOG=<file>]"
	@echo "  make gate       the same, --strict        ORG=<uuid> [LOG=<file>]"

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

# Component tests for the surfaces that call the API. jsdom, scripted `fetch`, no
# network and no provider — a frontend test that reached Vertex would bill CI.
test-web:
	pnpm --filter web test

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

# §21's M1 clause is "≥70% coverage on core/graph/retrieval", per package rather than
# blended — a well-covered jutsu_core must not be able to carry a thin jutsu_graph over
# the line. Deliberately NOT part of `preflight`: coverage instrumentation roughly
# doubles the suite's runtime and preflight runs on every commit. `make gate` is where
# the floor is enforced.
test-py-cov:
	uv run pytest -q --cov=jutsu_core --cov=jutsu_graph --cov=jutsu_retrieval \
		--cov-report=term-missing:skip-covered --cov-report=json:coverage.json

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
preflight: lint-web typecheck-web test-web test-hooks lint-py format-check-py typecheck-py test-py api-types-check
	@echo "preflight OK"

# ---------------------------------------------------------------- schema

# Both stores, in dependency order, and NOT one recipe with two lines.
#
# Make runs prerequisites left to right and aborts the whole target the moment one exits
# non-zero, so `make migrate` cannot report success having migrated Postgres and skipped
# Neo4j. Written as two lines inside one recipe it would still abort — each line is its
# own shell and make checks the status — but as separate targets the failure names which
# store broke, and either can be run alone during an incident.
#
# Postgres first: nothing in the graph depends on it today, but a schema change that
# needs both should land in the store that has transactions.
migrate: migrate-pg migrate-graph

migrate-pg:
	uv run --env-file .env --package jutsu-db alembic -c packages/db/alembic.ini upgrade head

# Numbered Cypher, a ledger node and checksum verification — see packages/graph/migrations.py.
migrate-graph:
	uv run --env-file .env --package jutsu-graph python -m jutsu_graph.cli upgrade

migrate-down: migrate-graph-down migrate-pg-down

migrate-pg-down:
	uv run --env-file .env --package jutsu-db alembic -c packages/db/alembic.ini downgrade base

migrate-graph-down:
	uv run --env-file .env --package jutsu-graph python -m jutsu_graph.cli downgrade

# What has been applied, in both stores. The first thing to run when a deploy disagrees
# with what you expected.
migrate-status:
	uv run --env-file .env --package jutsu-db alembic -c packages/db/alembic.ini current
	uv run --env-file .env --package jutsu-graph python -m jutsu_graph.cli current

migrate-rev:
	uv run --env-file .env --package jutsu-db alembic -c packages/db/alembic.ini revision --autogenerate -m "$(m)"

psql:
	docker compose -f infra/docker/compose.yml exec postgres psql -U jutsu -d jutsu

# ---------------------------------------------------------------- not yet implemented

# S8. Idempotent by construction: a second run lists the same identifiers, matches the
# same job keys and the same content hashes, and writes nothing.
#
# ORG and ROOT are required and there is no default corpus. A seed command that invented
# documents would put fabricated data behind every downstream measurement (S4.11), and the
# real Enron corpus is a deliberate act by an operator who has downloaded it (S19).
#
# Embedding is opt-in and costs money: add EMBED=--embed once you mean it.
seed:
	uv run --env-file .env --package jutsu-worker python -m jutsu_worker.cli seed --org "$(ORG)" --root "$(ROOT)" $(EMBED) $(SAMPLE) $(if $(LOG),--log-file "$(LOG)",) $(SEED_ARGS)

# §18 — the only sanctioned source of a number this project quotes (CLAUDE.md rule 8).
#
# `eval` is the report a human reads; `gate` is the same measurement with `--strict`, so
# a clause that could not be measured fails the run rather than being quietly absent from
# the tally. That difference is the whole point: on a laptop with the containers stopped
# `make eval` is informative and `make gate` is red, and both are honest.
#
# ORG is required by every tenant-scoped clause. LOG is optional and points at a captured
# seed-run log for the PII clause — see `make seed LOG=...`.
eval:
	uv run --env-file .env python scripts/gate.py --phase 1 --org "$(ORG)" $(if $(LOG),--log "$(LOG)",) $(GATE_ARGS)

gate:
	uv run --env-file .env python scripts/gate.py --phase 1 --org "$(ORG)" $(if $(LOG),--log "$(LOG)",) --strict $(GATE_ARGS)

deploy:
	@echo "deploy is implemented in S29. See docs/jutsu-master-spec.md"
	@exit 1

clean:
	pnpm --filter web exec rm -rf .next
