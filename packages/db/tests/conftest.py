"""Fixtures for the Postgres tests.

These need a real database — pgvector, RLS and `FORCE ROW LEVEL SECURITY` have no
in-memory equivalent, and a SQLite stand-in would pass while proving nothing about the
thing S1 exists to guarantee.

Two URLs, deliberately:

  * `JUTSU_TEST_DATABASE_URL` — the **restricted** role (NOSUPERUSER NOBYPASSRLS) that
    the application uses. Every isolation assertion runs through this one, because a
    superuser bypasses RLS unconditionally and the whole suite would pass against
    policies that never engage.
  * `JUTSU_TEST_MIGRATION_URL` — the owner, which is the only role with DDL rights.

Without them the tests skip, naming what is missing, so `make preflight` stays usable on
a machine without Docker. CI sets both against a pgvector service container. The S1 gate
requires zero skips.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"


#: Why this is a helper and not a `pytestmark`: a module-level `pytestmark` in a
#: **conftest** is inert. pytest honours it in test modules and classes only, so the one
#: that sat here for six migrations skipped nothing — the skips everyone saw came from
#: `pytest.skip()` inside the `database_url` fixture below, which is the mechanism that
#: actually works. Discovered when the guard was changed and the suite kept erroring.
def _skip_without_database() -> None:
    """Skip unless something is really listening.

    "Is the variable set" stopped being the same question once the root conftest began
    loading `.env`: the variable is now always set, so a stopped Postgres turned a clean
    skip into a hundred connection errors — which also blocked every commit, because the
    pre-commit hook runs preflight. The root conftest probes once and publishes the answer.
    """
    if os.environ.get("JUTSU_DB_REACHABLE") != "1":
        pytest.skip(f"nothing listening at {TEST_DB_ENV} — start Postgres with `make up`")


async def run_alembic(cfg: Config, direction: str, revision: str) -> None:
    """Run an Alembic command from inside an async test.

    Alembic's env.py is async and ends in `asyncio.run()`. Called directly from an
    async fixture that would raise "cannot be called from a running event loop", so the
    command goes to a worker thread where no loop is running yet.
    """
    fn = command.upgrade if direction == "upgrade" else command.downgrade
    await asyncio.to_thread(fn, cfg, revision)


def alembic_config(url: str) -> Config:
    """Alembic config pointed at the test database.

    `script_location` is resolved from this file rather than the process CWD, so the
    suite behaves the same whether pytest is invoked from the repo root or the package.
    """
    package_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(package_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(package_root / "src" / "jutsu_db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def database_url() -> str:
    """Connection as the restricted application role — what RLS is tested through."""
    _skip_without_database()
    url = os.environ.get(TEST_DB_ENV)
    if not url:
        pytest.skip(f"{TEST_DB_ENV} is unset")
    return url


@pytest.fixture(scope="session")
def migration_url(database_url: str) -> str:
    """Connection as the owner. Only this role can run DDL."""
    return os.environ.get(MIGRATION_DB_ENV, database_url)


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url, poolclass=None)
    yield eng
    await eng.dispose()


@pytest.fixture
async def migrated(
    database_url: str, migration_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[str]:
    """A database at head, torn back down afterwards.

    Migrations run through Alembic rather than `metadata.create_all` on purpose: the RLS
    policies, the HNSW index and the extension only exist in the migration, so
    create_all would produce a schema that looks complete and enforces nothing.
    """
    # DDL runs as the owner; the yielded URL is the restricted role the tests use.
    monkeypatch.setenv("DATABASE_URL", migration_url)
    cfg = alembic_config(migration_url)

    await run_alembic(cfg, "downgrade", "base")  # start from a known-empty state
    await run_alembic(cfg, "upgrade", "head")
    yield database_url
    await run_alembic(cfg, "downgrade", "base")


@pytest.fixture
async def conn(migrated: str) -> AsyncIterator[AsyncConnection]:
    eng = create_async_engine(migrated)
    async with eng.connect() as connection:
        yield connection
    await eng.dispose()


@pytest.fixture
async def two_orgs(conn: AsyncConnection) -> tuple[uuid.UUID, uuid.UUID]:
    """Two orgs, each with one source, one document, one chunk and one ACL grant.

    Seeded through a connection that sets the GUC per insert, because the RLS policies
    carry `WITH CHECK` — an insert whose `org_id` does not match the scope is rejected,
    which is itself part of what these tests verify.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()

    for org_id, label in ((org_a, "alpha"), (org_b, "beta")):
        # The scope is set BEFORE the organisation row exists, and that ordering is the
        # point rather than an accident. Migration 0002 put RLS on `orgs` itself, with a
        # `WITH CHECK` on `id`, so an unscoped INSERT is rejected — and the row cannot be
        # scoped to itself before it exists. The id is therefore minted client-side and
        # the GUC set to it first. Real registration has exactly the same shape: it
        # creates the tenant it is already scoped to, in one transaction.
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
        )

        await conn.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, :name)"),
            {"id": org_id, "name": label},
        )
        source_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json) "
                "VALUES (:id, :org, 'local', '{}'::jsonb)"
            ),
            {"id": source_id, "org": org_id},
        )

        doc_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO documents (id, org_id, source_id, external_id, title, "
                "content_hash, acl_hash, body_original, body_masked, created_at) "
                "VALUES (:id, :org, :src, :ext, :title, 'h', 'a', 'body', 'body', now())"
            ),
            {"id": doc_id, "org": org_id, "src": source_id, "ext": f"{label}-1", "title": label},
        )
        await conn.execute(
            text(
                "INSERT INTO chunks (id, document_id, org_id, ordinal, text, "
                "char_start, char_end, token_count) "
                "VALUES (:id, :doc, :org, 0, 'chunk text', 0, 10, 2)"
            ),
            {"id": uuid.uuid4(), "doc": doc_id, "org": org_id},
        )
        await conn.execute(
            text(
                "INSERT INTO document_acl (document_id, principal_type, principal_id, "
                "org_id, permission) VALUES (:doc, 'user', :pid, :org, 'read')"
            ),
            # Namespaced, as ADR 0010 requires: a bare subject is meaningless outside the
            # system that issued it.
            {"doc": doc_id, "pid": f"local:user-{label}", "org": org_id},
        )

        # One user, one source identity and one group per organisation, so migration
        # 0008's two new RLS tables carry exactly one row each per tenant — which is what
        # `test_counts_do_not_leak` needs in order to cover them rather than skip them.
        user_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO users (id, org_id, email, status) VALUES (:id, :org, :email, 'active')"
            ),
            {"id": user_id, "org": org_id, "email": f"{label}@example.com"},
        )
        await conn.execute(
            text(
                "INSERT INTO source_identities (org_id, user_id, source_system, subject) "
                "VALUES (:org, :user, 'local', :subject)"
            ),
            {"org": org_id, "user": user_id, "subject": f"user-{label}"},
        )
        await conn.execute(
            text(
                "INSERT INTO user_groups (user_id, org_id, group_external_id) "
                "VALUES (:user, :org, :group)"
            ),
            {"user": user_id, "org": org_id, "group": f"group-{label}"},
        )

        # One job per organisation, for the same reason as the two rows above: migration
        # 0010 put `jobs` and `sources` under the policy, and `test_counts_do_not_leak`
        # covers a table only if each tenant actually holds a row in it. `sources` already
        # has one from the seeding above; the queue did not.
        await conn.execute(
            text(
                "INSERT INTO jobs (id, org_id, kind, state, idempotency_key) "
                "VALUES (:id, :org, 'ingest.document', 'pending', :key)"
            ),
            {"id": uuid.uuid4(), "org": org_id, "key": f"ingest.document:{org_id}:seed"},
        )

    await conn.commit()
    return org_a, org_b
