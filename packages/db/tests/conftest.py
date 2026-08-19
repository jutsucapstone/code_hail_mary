"""Fixtures for the Postgres tests.

These need a real database — pgvector, RLS and `FORCE ROW LEVEL SECURITY` have no
in-memory equivalent, and a SQLite stand-in would pass while proving nothing about the
thing S1 exists to guarantee.

Without `JUTSU_TEST_DATABASE_URL` the tests skip with a message naming what is missing,
so `make preflight` stays usable on a machine without Docker. CI sets the variable
against a pgvector service container, so they run for real there. The S1 gate requires
zero skips.
"""

from __future__ import annotations

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

pytestmark = pytest.mark.skipif(
    not os.environ.get(TEST_DB_ENV),
    reason=f"{TEST_DB_ENV} is unset — start Postgres with `make up` and export it",
)


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
    url = os.environ.get(TEST_DB_ENV)
    if not url:
        pytest.skip(f"{TEST_DB_ENV} is unset")
    return url


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url, poolclass=None)
    yield eng
    await eng.dispose()


@pytest.fixture
async def migrated(database_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """A database at head, torn back down afterwards.

    Migrations run through Alembic rather than `metadata.create_all` on purpose: the RLS
    policies, the HNSW index and the extension only exist in the migration, so
    create_all would produce a schema that looks complete and enforces nothing.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)
    cfg = alembic_config(database_url)

    command.downgrade(cfg, "base")  # start from a known-empty state
    command.upgrade(cfg, "head")
    yield database_url
    command.downgrade(cfg, "base")


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

        # RLS is FORCEd, so even seeding must be scoped.
        await conn.execute(text("SET LOCAL app.current_org_id = :org"), {"org": str(org_id)})

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
            {"doc": doc_id, "pid": f"user-{label}", "org": org_id},
        )

    await conn.commit()
    return org_a, org_b
