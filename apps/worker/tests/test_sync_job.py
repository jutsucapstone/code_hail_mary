"""The connector.sync stage: a sync becomes a source walk.

What must hold: a queued sync is CLAIMED and RESOLVED — never left pending forever and
never completed as if content moved when it did not. A provider with a real connector
gets a sources row and an ingest.source job (the existing pipeline IS the sync path);
a provider without one still fails honestly as source_unavailable, audited, with the
connection annotated sync_unavailable for its owner's UI.
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
    """One org, one user, one connected github connection, one queued sync — exactly the
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
                "INSERT INTO connections (id, org_id, user_id, provider, status, "
                "provider_subject) VALUES (:id, :org, :user, 'github', 'connected', "
                "'583231')"
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
    async def test_a_queued_sync_enqueues_the_walk_and_completes(self) -> None:
        org_id = uuid.uuid4()
        connection_id, job_id = await seed_connection_and_job(org_id)

        outcome = await process_connector_sync(org_id, job_id=job_id)
        assert outcome == 1, "one walk enqueued"

        async with org_session(org_id) as session:
            job = (
                await session.execute(text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id})
            ).one()
            assert job.state == "completed"

            source = (
                await session.execute(
                    text(
                        "SELECT id, system::text AS system, config_json FROM sources "
                        "WHERE config_json->>'connection_id' = :cid"
                    ),
                    {"cid": str(connection_id)},
                )
            ).one()
            assert source.system == "github", "the ACL namespace, not the provider id"
            assert source.config_json["provider"] == "github"

            walk = (
                await session.execute(
                    text(
                        "SELECT state FROM jobs WHERE kind = 'ingest.source' "
                        "AND payload_json->>'source_id' = :sid"
                    ),
                    {"sid": str(source.id)},
                )
            ).one()
            assert walk.state == "pending"

    async def test_a_second_sync_reuses_the_source_and_reopens_the_walk(self) -> None:
        org_id = uuid.uuid4()
        connection_id, job_id = await seed_connection_and_job(org_id)
        await process_connector_sync(org_id, job_id=job_id)

        async with org_session(org_id) as session:
            await session.execute(
                text("UPDATE jobs SET state = 'completed' WHERE kind = 'ingest.source'")
            )
            await session.execute(
                text("UPDATE jobs SET state = 'pending', attempts = 0 WHERE id = :id"),
                {"id": job_id},
            )

        outcome = await process_connector_sync(org_id, job_id=job_id)
        assert outcome == 1

        async with org_session(org_id) as session:
            sources = (
                await session.execute(
                    text("SELECT count(*) FROM sources WHERE config_json->>'connection_id' = :cid"),
                    {"cid": str(connection_id)},
                )
            ).scalar_one()
            assert sources == 1, "one source per connection, however many syncs"
            walk = (
                await session.execute(
                    text("SELECT state, attempts FROM jobs WHERE kind = 'ingest.source'")
                )
            ).one()
            assert walk.state == "pending", "the completed walk was reopened"
            assert walk.attempts == 0

    async def test_an_unimplemented_provider_still_fails_honestly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jutsu_worker.sync as sync_module

        monkeypatch.setattr(sync_module, "CONNECTOR_CLASSES", {})
        org_id = uuid.uuid4()
        connection_id, job_id = await seed_connection_and_job(org_id)

        outcome = await process_connector_sync(org_id, job_id=job_id)
        assert outcome is JOB_FAILED

        async with org_session(org_id) as session:
            job = (
                await session.execute(
                    text("SELECT state, failure_kind FROM jobs WHERE id = :id"),
                    {"id": job_id},
                )
            ).one()
            assert job.failure_kind == "source_unavailable"
            assert job.state in ("failed", "dead_letter")

            row = (
                await session.execute(
                    text("SELECT status, last_error_kind FROM connections WHERE id = :id"),
                    {"id": connection_id},
                )
            ).one()
            assert row.last_error_kind == "sync_unavailable"
            assert row.status == "connected"

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
        assert await process_connector_sync(org_id) is None


class TestDrainOrg:
    """The per-org drain — the worker half of the doorbell (ADR 0012)."""

    async def test_one_drain_carries_sync_into_the_walk_which_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No JUTSU_CONNECTION_KEY in this environment, so the walk must fail closed as
        a deployment problem (provider_permanent) — never fetch, never fabricate."""
        from jutsu_worker.runner import drain_org

        monkeypatch.delenv("JUTSU_CONNECTION_KEY", raising=False)
        org_id = uuid.uuid4()
        _connection_id, job_id = await seed_connection_and_job(org_id)

        counts = await drain_org(org_id)
        assert counts["connector.sync"] == 1, "the queued sync was claimed and resolved"
        assert counts["ingest.source"] == 1, "the walk it enqueued ran in the same drain"

        async with org_session(org_id) as session:
            sync_job = (
                await session.execute(text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id})
            ).one()
            assert sync_job.state == "completed"
            walk = (
                await session.execute(
                    text("SELECT state, failure_kind FROM jobs WHERE kind = 'ingest.source'")
                )
            ).one()
            assert walk.failure_kind == "provider_permanent"

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


class TestDrainFollowUp:
    """A retry with a future next_attempt_at has no doorbell of its own — the drain
    dispatch re-rings for it, deferred, with a deterministic job id."""

    async def test_waiting_retries_re_ring_the_doorbell(self) -> None:
        from jutsu_worker.main import drain_org_jobs

        org_id = uuid.uuid4()
        async with org_session(org_id) as session:
            await session.execute(
                text("INSERT INTO orgs (id, name) VALUES (:id, 'retry-org')"), {"id": org_id}
            )
            await session.execute(
                text(
                    "INSERT INTO jobs (id, org_id, kind, state, idempotency_key, "
                    "payload_json, next_attempt_at) VALUES (:id, :org, 'embed.document', "
                    "'retry_scheduled', :key, cast(:p AS jsonb), now() + interval '5 minutes')"
                ),
                {
                    "id": uuid.uuid4(),
                    "org": str(org_id),
                    "key": f"embed.document:{org_id}:{uuid.uuid4()}",
                    "p": '{"document_id": "irrelevant"}',
                },
            )

        class RecordingRedis:
            def __init__(self) -> None:
                self.enqueued: list[tuple[str, str]] = []

            async def enqueue_job(self, name: str, *args: str, **kwargs: object) -> None:
                self.enqueued.append((name, str(kwargs.get("_job_id"))))

        redis = RecordingRedis()
        await drain_org_jobs({"redis": redis}, str(org_id))
        assert redis.enqueued == [("drain_org_jobs", f"drain-retry:{org_id}")]

    async def test_claimable_leftovers_re_ring_almost_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A drain stopped at its soft deadline (or killed at arq's hard one) leaves
        pending work; the follow-up must not wait a backoff it does not owe."""
        from jutsu_worker.main import drain_org_jobs

        org_id = uuid.uuid4()
        _connection_id, _job_id = await seed_connection_and_job(org_id)

        async def out_of_time(org: uuid.UUID, **kwargs: object) -> dict[str, int]:
            # The soft deadline elapsed before anything was claimed.
            return {"connector.sync": 0}

        monkeypatch.setattr("jutsu_worker.main.drain_org", out_of_time)

        class RecordingRedis:
            def __init__(self) -> None:
                self.enqueued: list[tuple[str, str]] = []

            async def enqueue_job(self, name: str, *args: str, **kwargs: object) -> None:
                self.enqueued.append((name, str(kwargs.get("_job_id"))))

        redis = RecordingRedis()
        await drain_org_jobs({"redis": redis}, str(org_id))
        assert redis.enqueued == [("drain_org_jobs", f"drain-more:{org_id}")]

    async def test_an_idle_org_rings_nothing(self) -> None:
        from jutsu_worker.main import drain_org_jobs

        org_id = uuid.uuid4()
        async with org_session(org_id) as session:
            await session.execute(
                text("INSERT INTO orgs (id, name) VALUES (:id, 'quiet-org')"), {"id": org_id}
            )

        class RefusingRedis:
            async def enqueue_job(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("no follow-up was warranted")

        await drain_org_jobs({"redis": RefusingRedis()}, str(org_id))
