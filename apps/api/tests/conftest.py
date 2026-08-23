"""Fixtures for the API integration tests.

These run against a real Postgres, because everything worth testing here is enforced by
the database: the `auth` schema's privilege boundary, the atomic attempt budget, and
row-level security. A mocked session would exercise none of it and would pass against a
design that leaks.

The schema is migrated and torn down per test rather than shared. It costs a second or so
and buys independence from `packages/db`'s own suite, which downgrades to base at the end
of each of its tests — without this, whichever ran second would find no tables.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from jutsu_api.config import Settings
from jutsu_api.email import RecordingEmailSender
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"


#: Not a `pytestmark`: one of those in a **conftest** is inert — pytest honours it in
#: test modules and classes only. The skip has to come from the fixture, which is where
#: it always effectively came from.
def _skip_without_database() -> None:
    """Skip unless something is really listening.

    "Is the variable set" stopped being the same question once the root conftest began
    loading `.env`: the variable is always set now, so a stopped Postgres turned a clean
    skip into a hundred connection errors that also blocked every commit.
    """
    if os.environ.get("JUTSU_DB_REACHABLE") != "1":
        pytest.skip(f"nothing listening at {TEST_DB_ENV} — start Postgres with `make up`")


def _alembic_config(url: str) -> Config:
    package_root = Path(__file__).resolve().parents[3] / "packages" / "db"
    cfg = Config(str(package_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(package_root / "src" / "jutsu_db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _run_alembic(cfg: Config, direction: str, revision: str) -> None:
    """Alembic's env.py ends in `asyncio.run`, which cannot nest inside a running loop."""
    fn = command.upgrade if direction == "upgrade" else command.downgrade
    await asyncio.to_thread(fn, cfg, revision)


@pytest.fixture
def database_url() -> str:
    _skip_without_database()
    url = os.environ.get(TEST_DB_ENV)
    if not url:
        pytest.skip(f"{TEST_DB_ENV} is unset")
    return url


@pytest.fixture
def migration_url(database_url: str) -> str:
    """Only the owner may run DDL; the application role deliberately cannot."""
    return os.environ.get(MIGRATION_DB_ENV, database_url)


@pytest.fixture
async def db_session(
    database_url: str, migration_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncSession]:
    monkeypatch.setenv("DATABASE_URL", migration_url)
    cfg = _alembic_config(migration_url)

    await _run_alembic(cfg, "downgrade", "base")
    await _run_alembic(cfg, "upgrade", "head")

    # Connects as the restricted application role, which is the whole point: a superuser
    # bypasses RLS unconditionally and every isolation assertion below would be vacuous.
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session

    await engine.dispose()
    await _run_alembic(cfg, "downgrade", "base")


@pytest.fixture
async def inspector(migration_url: str) -> AsyncIterator[AsyncSession]:
    """A privileged read-only view of the database, for assertions only.

    Every assertion of the form "no organisation exists yet" is **vacuous** on
    `db_session`. That connects as `jutsu_app`, which is subject to row-level security,
    so a `SELECT count(*) FROM orgs` with no tenant scope set returns 0 whether or not a
    row was written — the test would pass just as happily against code that created the
    organisation it was supposed to prevent.

    Found by running the suite: `test_staging_creates_no_organisation_and_no_user` passed
    while proving nothing, which is the most expensive kind of green.

    So: connect as the owner, which is a superuser and therefore bypasses RLS
    unconditionally. Never use this to *exercise* the system — doing that would make the
    isolation tests meaningless in exactly the way the `db_session` docstring warns
    about. It exists to look, never to act.

    **AUTOCOMMIT, and that is load-bearing.** A second connection holding an open
    transaction blocks the `downgrade base` in `db_session`'s teardown, which needs an
    exclusive lock to drop the tables — the suite then hangs rather than failing, with no
    output to explain it. Found by watching a run sit at twenty minutes.
    """
    engine = create_async_engine(migration_url, isolation_level="AUTOCOMMIT")
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session
        await session.rollback()
    await engine.dispose()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A deterministic pepper, so the HMAC is stable within a test run.

    Not a realistic value and not a default — `get_settings()` refuses to invent one,
    because an identical pepper across deployments would make the org-less identity table
    correlatable between them.
    """
    monkeypatch.setenv("JUTSU_EMAIL_PEPPER", "test-pepper-not-a-real-secret")
    return Settings(email_pepper=b"test-pepper-not-a-real-secret", environment="test")


@pytest.fixture
def mailbox() -> RecordingEmailSender:
    return RecordingEmailSender()
