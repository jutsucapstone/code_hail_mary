"""The connector.sync stage: honest failure until a fetcher exists.

What must hold: a queued sync is CLAIMED and RESOLVED — never left pending forever and
never completed as if a sync happened. With no fetcher it fails as `source_unavailable`
(non-retryable, so it goes straight past the retry ladder), the failure is audited, and
the connection is annotated `sync_unavailable` for its owner's UI.
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
from jutsu_db.engine import dispose_engine, org_session
from jutsu_worker.runner import JOB_FAILED, process_connector_sync
from sqlalchemy import text

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"

pytestmark = pytest.mark.usefixtures("worker_database")


def _alembic_config(url: str) -> Config:
    root = Path(__file__).resolve().parents[3] / "packages" / "db"
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src" / "jutsu_db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
async def worker_database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Migrated schema, app-role engine, disposed on BOTH sides — the process-cached
    engine trap, same as test_ingest_pipeline. Inline rather than in a conftest because
    mypy refuses a second module named conftest under apps/."""
    if os.environ.get("JUTSU_DB_REACHABLE") != "1":
        pytest.skip(f"nothing listening at {TEST_DB_ENV}")
    app_url = os.environ[TEST_DB_ENV]
    migration_url = os.environ.get(MIGRATION_DB_ENV, app_url)

    cfg = _alembic_config(migration_url)
    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")

    monkeypatch.setenv("DATABASE_URL", app_url)
    await dispose_engine()
    yield
    await dispose_engine()
    await asyncio.to_thread(command.downgrade, cfg, "base")


async def seed_connection_and_job(org_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """One org, one user, one connected slack connection, one queued sync — exactly the
    rows POST /v1/me/connections/{id}/sync leaves behind."""
    user_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    job_id = uuid.uuid4()

    async with org_session(org_id) as session:
        await session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, 'sync-test')"), {"id": org_id}
        )
        await session.execute(
            text(
                "INSERT INTO users (id, org_id, email, status) "
                "VALUES (:id, :org, 'owner@sync.test', 'active')"
            ),
            {"id": user_id, "org": org_id},
        )
        await session.execute(
            text(
                "INSERT INTO connections (id, org_id, user_id, provider, status) "
                "VALUES (:id, :org, :user, 'slack', 'connected')"
            ),
            {"id": connection_id, "org": org_id, "user": user_id},
        )
        await session.execute(
            text(
                "INSERT INTO jobs (id, org_id, kind, state, idempotency_key, payload_json) "
                "VALUES (:id, :org, 'connector.sync', 'pending', :key, "
                "cast(:payload AS jsonb))"
            ),
            {
                "id": job_id,
                "org": str(org_id),
                "key": f"connector.sync:{connection_id}",
                "payload": f'{{"connection_id": "{connection_id}"}}',
            },
        )
    return connection_id, job_id


class TestSyncJob:
    async def test_a_queued_sync_resolves_instead_of_waiting_forever(self) -> None:
        org_id = uuid.uuid4()
        _connection_id, job_id = await seed_connection_and_job(org_id)

        outcome = await process_connector_sync(org_id, job_id=job_id)
        assert outcome is JOB_FAILED, "with no fetcher, the honest outcome is a failure"

        async with org_session(org_id) as session:
            job = (
                await session.execute(
                    text("SELECT state, failure_kind FROM jobs WHERE id = :id"),
                    {"id": job_id},
                )
            ).one()
            # Non-retryable classification: no amount of retrying invents a fetcher.
            assert job.failure_kind == "source_unavailable"
            assert job.state in ("failed", "dead_letter")
            assert job.state != "pending"

    async def test_the_connection_is_annotated_for_its_owner(self) -> None:
        org_id = uuid.uuid4()
        connection_id, job_id = await seed_connection_and_job(org_id)

        await process_connector_sync(org_id, job_id=job_id)

        async with org_session(org_id) as session:
            row = (
                await session.execute(
                    text("SELECT status, last_error_kind FROM connections WHERE id = :id"),
                    {"id": connection_id},
                )
            ).one()
            assert row.last_error_kind == "sync_unavailable"
            # The connection itself is fine — the sync failed, not the authorization.
            assert row.status == "connected"

    async def test_the_failure_is_audited_without_error_text(self) -> None:
        org_id = uuid.uuid4()
        _, job_id = await seed_connection_and_job(org_id)

        await process_connector_sync(org_id, job_id=job_id)

        async with org_session(org_id) as session:
            entry = (
                await session.execute(
                    text(
                        "SELECT action, outcome, meta_json FROM audit_log "
                        "WHERE action = 'connector.sync.failed'"
                    )
                )
            ).one()
            assert entry.outcome == "failure"
            assert entry.meta_json["failure_kind"] == "source_unavailable"

    async def test_an_empty_queue_is_none_not_a_failure(self) -> None:
        org_id = uuid.uuid4()
        async with org_session(org_id) as session:
            await session.execute(
                text("INSERT INTO orgs (id, name) VALUES (:id, 'idle')"), {"id": org_id}
            )

        outcome = await process_connector_sync(org_id)
        assert outcome is None


class TestDrainOrg:
    """The per-org drain — the worker half of the doorbell (ADR 0012)."""

    async def test_one_drain_resolves_the_backlog_and_then_reports_idle(self) -> None:
        from jutsu_worker.runner import drain_org

        org_id = uuid.uuid4()
        _connection_id, job_id = await seed_connection_and_job(org_id)

        counts = await drain_org(org_id)
        assert counts["connector.sync"] == 1, "the queued sync was claimed and resolved"

        async with org_session(org_id) as session:
            job = (
                await session.execute(text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id})
            ).one()
            assert job.state in ("failed", "dead_letter")

        # A second drain finds nothing claimable and stops instead of spinning.
        again = await drain_org(org_id)
        assert sum(again.values()) == 0

    async def test_a_drain_for_one_org_never_claims_anothers_jobs(self) -> None:
        from jutsu_worker.runner import drain_org

        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        _conn_a, job_a = await seed_connection_and_job(org_a)

        counts = await drain_org(org_b)
        assert sum(counts.values()) == 0, "org B holds no jobs, and A's are invisible to it"

        async with org_session(org_a) as session:
            job = (
                await session.execute(text("SELECT state FROM jobs WHERE id = :id"), {"id": job_a})
            ).one()
            assert job.state == "pending", "another org's drain must not have touched it"
