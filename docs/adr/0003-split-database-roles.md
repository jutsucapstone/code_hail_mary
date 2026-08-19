# ADR 0003 — The application connects as a role that cannot bypass RLS

*Status: accepted · 2026-08-19 · slice S1*

## Context

§8 requires row-level security "enabled **and** forced" on `documents`, `chunks` and
`document_acl`, and §4.5 requires ACL filtering to happen inside the SQL query. Migration
0001 emits both `ENABLE` and `FORCE ROW LEVEL SECURITY` plus a policy per table.

That is not sufficient. Postgres exempts two things from row-level security:

1. Any role with `rolsuper` or `rolbypassrls`. The exemption is unconditional; no table
   setting overrides it.
2. The table owner — unless `FORCE ROW LEVEL SECURITY` is set, which is why we set it.

The Compose bootstrap role (`POSTGRES_USER: jutsu`) is a superuser. Running the
application and the tests through it means every policy is skipped, every cross-org
isolation assertion passes against an unenforced predicate, and the suite is green on a
guarantee that does not exist. This was verified, not assumed: `pg_roles` reports
`jutsu: rolsuper=t rolbypassrls=t`.

The failure is invisible in exactly the environments where it is cheapest to catch, and
appears first in the environment where it costs the most — a deployment that happens to
use a restricted role behaves differently from every test that ever ran.

## Decision

Two roles, in every environment including local dev.

| Role | Grants | Used by |
|---|---|---|
| `jutsu` | owner, superuser in dev | Alembic migrations only |
| `jutsu_app` | `NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT` | the application, and every test that asserts isolation |

- `infra/docker/initdb/01-app-role.sql` creates `jutsu_app` and the `jutsu_test`
  database on first initialisation of the data volume. CI creates the same role from a
  step, because service containers start before checkout and cannot mount the script.
- Table privileges are granted by migration 0001, which is the thing that knows what
  tables exist.
- `DATABASE_URL` is the restricted role. Alembic reads `MIGRATION_DATABASE_URL` first and
  falls back to `DATABASE_URL`, so a deployment that legitimately uses one role still
  works while dev and CI keep them apart.
- `test_app_role_cannot_bypass_rls` asserts `rolsuper is False and rolbypassrls is False`
  for `current_user`. It is the test that makes the rest of the isolation suite mean
  something: if a future change points the tests back at the owner, that one fails first
  and names the reason.

## Consequences

- Running the suite needs two URLs rather than one. `conftest.py` skips with a message
  naming the missing variable, so `make preflight` stays usable without Docker — but the
  S1 gate requires zero skips, and CI asserts the DB tests did not skip.
- The app role cannot run DDL. This is the point, and it is verified: pointing Alembic at
  `jutsu_app` fails with `must be owner of relation documents`.
- Staging and production create `jutsu_app` through Terraform with a Secret Manager
  password (§4.10, §20). Only the shape is shared with dev; the credential is not.

## Alternatives rejected

- **`ALTER ROLE jutsu NOSUPERUSER`.** Migrations need DDL and the bootstrap role needs
  superuser to `CREATE EXTENSION vector`. Demoting it breaks `make up`.
- **Test the policies with `SET ROLE`.** Proves the predicate, but leaves the application
  connecting as a superuser, which is the actual leak. The test would pass and production
  would still be unprotected.
- **Rely on `FORCE ROW LEVEL SECURITY` alone.** FORCE covers the owner, not superusers.
  This is the misreading the ADR exists to record.
