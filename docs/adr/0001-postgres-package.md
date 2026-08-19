# ADR 0001 — Postgres persistence lives in `packages/db`

- **Status:** accepted
- **Date:** 2026-08-19
- **Slice:** S1

## Context

Spec §6 enumerates seven packages, and none of them owns the Postgres schema:

| Package | Scope per §6 |
|---|---|
| `core` | Pydantic domain models, enums, typed errors, PII, chunking, jobs |
| `graph` | **Neo4j** driver, Cypher templates, migrations, temporal helpers |
| `retrieval` | Embeddings, **pgvector search**, fusion, rerank, ACL filter |
| `agents`, `connectors`, `evals`, `ui` | unrelated |

S1 must create fifteen tables (§8), only two of which — `chunks` and `document_acl` —
are retrieval concerns. `orgs`, `users`, `sources`, `jobs`, `audit_log`,
`extraction_runs` and the rest are not.

## Options

1. **`packages/core`.** Rejected. Core is declared pure types, import-cheap and
   side-effect free, and every other package depends on it. Adding SQLAlchemy, asyncpg
   and Alembic there forces those dependencies on `connectors`, `evals` and `ui`
   consumers that never touch a database.
2. **`packages/retrieval`.** Rejected. It would make the retrieval package the owner of
   `audit_log` and `jobs`, which have no retrieval semantics. It also inverts the
   dependency we want: retrieval should depend on the schema, not define it.
3. **New `packages/db`.** Accepted.

## Decision

Add `packages/db` as an eighth package, owning the async engine, session factory,
SQLAlchemy models and Alembic migrations. `retrieval` will depend on it and keep
ownership of the *queries* — including the ACL-filtered search of §12 — so the split is
"schema and connection" versus "how we search it".

## Consequences

- §6's package list is now eight, not seven. This ADR is the record of why.
- `packages/graph` stays exactly as §6 describes it: Neo4j only. Nothing about S1
  changes S2.
- `jutsu-db` becomes a workspace dependency of `api`, `worker` and `retrieval`.
- If a future slice finds itself importing `jutsu_db` from `core`, that is a signal the
  dependency direction has inverted and should be reviewed, not worked around.
