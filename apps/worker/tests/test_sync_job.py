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

    async def test_an_exhausted_deadline_claims_nothing(self) -> None:
        """`max_seconds` keeps the drain under arq's hard timeout, which would kill it
        mid-provider-call. At zero the loop must not claim even one job — a bound that
        lets one more claim through is advisory, and advisory is how a drain dies
        holding a lease."""
        from jutsu_worker.runner import drain_org

        org_id = uuid.uuid4()
        _connection_id, job_id = await seed_connection_and_job(org_id)

        counts = await drain_org(org_id, max_seconds=0)
        assert sum(counts.values()) == 0

        async with org_session(org_id) as session:
            job = (
                await session.execute(text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id})
            ).one()
            assert job.state == "pending", "the job waits for a drain with time to run it"

    async def test_an_orphaned_lease_is_reclaimed_before_the_deadline_gate(self) -> None:
        """A predecessor killed at arq's hard timeout leaves a working state, an expired
        lease and no transaction holding the row. Reclaim runs FIRST, outside the
        deadline loop — even a drain with no time left returns the job to the queue,
        rather than skipping it until the org's next walk happens to run."""
        from jutsu_worker.runner import drain_org

        org_id = uuid.uuid4()
        _connection_id, job_id = await seed_connection_and_job(org_id)
        async with org_session(org_id) as session:
            await session.execute(
                text(
                    "UPDATE jobs SET state = 'fetching', attempts = 1, "
                    "locked_until = now() - interval '1 hour' WHERE id = :id"
                ),
                {"id": job_id},
            )

        counts = await drain_org(org_id, max_seconds=0)
        assert sum(counts.values()) == 0
        async with org_session(org_id) as session:
            job = (
                await session.execute(text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id})
            ).one()
            assert job.state == "retry_scheduled", "reclaimed, not skipped"

        counts = await drain_org(org_id)
        assert counts["connector.sync"] == 1, "the reclaimed job was claimed and run"
        async with org_session(org_id) as session:
            job = (
                await session.execute(text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id})
            ).one()
            assert job.state == "completed"


class TestReauthAnnotation:
    """A dead grant surfaces inside the WALK, not the connector.sync job: the token is
    fetched when the connector first calls the provider, and by then the sync job has
    completed. The failure path must still reach the connection row, or its owner keeps
    a Sync button that can only fail where Reconnect belongs."""

    async def test_a_walk_that_dies_at_the_providers_door_flips_the_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jutsu_worker.fetchers as fetchers
        from jutsu_worker.credentials import ReauthRequired
        from jutsu_worker.runner import drain_org

        async def refuse(session: object, *, connection_id: uuid.UUID) -> str:
            raise ReauthRequired("The provider rejected the refresh token.")

        monkeypatch.setattr(fetchers, "access_token_for", refuse)

        org_id = uuid.uuid4()
        connection_id, _job_id = await seed_connection_and_job(org_id)

        counts = await drain_org(org_id)
        assert counts["connector.sync"] == 1
        assert counts["ingest.source"] == 1, "the walk is where the token is first used"

        async with org_session(org_id) as session:
            walk = (
                await session.execute(
                    text("SELECT state, failure_kind FROM jobs WHERE kind = 'ingest.source'")
                )
            ).one()
            assert walk.failure_kind == "provider_permanent"
            assert walk.state == "failed", "no retry can revive a revoked grant"

            row = (
                await session.execute(
                    text("SELECT status, last_error_kind FROM connections WHERE id = :id"),
                    {"id": connection_id},
                )
            ).one()
            assert row.status == "reauth_required"
            assert row.last_error_kind == "reauth_required"


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
        from jutsu_worker import drain as drain_module
        from jutsu_worker.main import drain_org_jobs

        org_id = uuid.uuid4()
        _connection_id, _job_id = await seed_connection_and_job(org_id)

        async def out_of_time(org: uuid.UUID, **kwargs: object) -> dict[str, int]:
            # Twelve jobs ran, then the soft deadline elapsed with one still claimable.
            # The counts have to be non-zero: a drain that claimed *nothing* is the
            # livelock case, and it must not ring itself again at once.
            return {"connector.sync": 12}

        # `drain_org_jobs` reaches `drain_org` through the shared drain now, not
        # through its own module (ADR 0017), so that is where the stub belongs.
        monkeypatch.setattr(drain_module, "drain_org", out_of_time)

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


@pytest.mark.usefixtures("worker_database")
class TestDrainReport:
    """The follow-up decision both dispatchers share (ADR 0017), against the real table."""

    async def test_an_empty_queue_needs_no_follow_up(self) -> None:
        from jutsu_worker.drain import drain_and_report

        report = await drain_and_report(uuid.uuid4())
        assert (report.claimable_now, report.retries_waiting, report.follow_up) == (0, 0, None)

    async def test_a_retry_that_is_not_due_yet_rings_after_the_backoff(self) -> None:
        from jutsu_worker.drain import drain_and_report

        org_id = uuid.uuid4()
        _connection_id, job_id = await seed_connection_and_job(org_id)
        async with org_session(org_id) as session:
            await session.execute(
                text(
                    "UPDATE jobs SET state = 'retry_scheduled', attempts = 1, "
                    "next_attempt_at = now() + interval '1 hour' WHERE id = :id"
                ),
                {"id": job_id},
            )

        report = await drain_and_report(org_id)

        assert sum(report.counts.values()) == 0, "not due, so not claimed"
        assert report.retries_waiting == 1
        assert report.follow_up == "retry"

    async def test_work_the_drain_could_not_finish_rings_again_at_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A drain that stops at its deadline leaves claimable rows; the report says so
        and the follow-up is immediate. The stub returns the jobs it got through before
        the deadline — an all-zero count would be a drain that claimed nothing, which is
        a different thing entirely and is covered below."""
        from jutsu_worker import drain as drain_module

        org_id = uuid.uuid4()
        await seed_connection_and_job(org_id)

        async def stopped_short(org: uuid.UUID) -> dict[str, int]:
            return {"connector.sync": 9}

        monkeypatch.setattr(drain_module, "drain_org", stopped_short)
        report = await drain_module.drain_and_report(org_id)

        assert report.claimable_now == 1
        assert report.follow_up == "now"

    async def test_work_the_drain_cannot_run_does_not_ring_for_ever(self) -> None:
        """`claimable_now` counts rows in a claimable *state*, not work this drain can do.

        `drain_org` skips embedding entirely when no provider is configured, so those
        rows sit `pending` — correct and documented. What must not follow is a follow-up
        every five seconds against a table nothing changed: measured locally, a two-file
        ingest with no Vertex left two `embed.document` rows and the door re-rang itself
        on every one of them. Progress is the evidence that ringing again is worth
        anything; without it the recovery path is the next real doorbell.
        """
        from jutsu_worker import drain as drain_module

        org_id = uuid.uuid4()
        async with org_session(org_id) as session:
            await session.execute(
                text("INSERT INTO orgs (id, name) VALUES (:id, 'no-embedder')"), {"id": org_id}
            )
            await session.execute(
                text(
                    "INSERT INTO jobs (id, org_id, kind, state, idempotency_key, "
                    "payload_json) VALUES (:id, :org, 'embed.document', 'pending', :key, "
                    "cast(:p AS jsonb))"
                ),
                {
                    "id": uuid.uuid4(),
                    "org": str(org_id),
                    "key": f"embed.document:{org_id}:{uuid.uuid4()}",
                    "p": '{"document_id": "irrelevant"}',
                },
            )

        async def claimed_nothing(org: uuid.UUID) -> dict[str, int]:
            return {"embed.document": 0}

        mp = pytest.MonkeyPatch()
        mp.setattr(drain_module, "drain_org", claimed_nothing)
        try:
            report = await drain_module.drain_and_report(org_id)
        finally:
            mp.undo()

        assert report.claimable_now == 1, "the row is genuinely claimable-by-state"
        assert report.follow_up is None, "but nothing moved, so ringing again buys nothing"

    async def test_a_stalled_drain_still_rings_for_a_waiting_retry(self) -> None:
        """No progress does not silence a retry that has its own due time."""
        from jutsu_worker import drain as drain_module

        org_id = uuid.uuid4()
        _connection_id, job_id = await seed_connection_and_job(org_id)
        async with org_session(org_id) as session:
            await session.execute(
                text(
                    "UPDATE jobs SET state = 'retry_scheduled', attempts = 1, "
                    "next_attempt_at = now() + interval '1 hour' WHERE id = :id"
                ),
                {"id": job_id},
            )

        async def claimed_nothing(org: uuid.UUID) -> dict[str, int]:
            return {}

        mp = pytest.MonkeyPatch()
        mp.setattr(drain_module, "drain_org", claimed_nothing)
        try:
            report = await drain_module.drain_and_report(org_id)
        finally:
            mp.undo()

        assert report.follow_up == "retry"

    async def test_another_tenant_s_backlog_is_invisible_to_the_report(self) -> None:
        """The org id is a hint, never an authorization: a report for one organisation
        counts nothing that belongs to another, so it cannot ring on their behalf."""
        from jutsu_worker import drain as drain_module

        org_a, org_b = uuid.uuid4(), uuid.uuid4()
        await seed_connection_and_job(org_a)

        async def nothing(org: uuid.UUID) -> dict[str, int]:
            return {}

        import pytest as _pytest

        mp = _pytest.MonkeyPatch()
        mp.setattr(drain_module, "drain_org", nothing)
        try:
            assert (await drain_module.drain_and_report(org_b)).claimable_now == 0
            assert (await drain_module.drain_and_report(org_a)).claimable_now == 1
        finally:
            mp.undo()


async def _not_rung(org_id: uuid.UUID) -> bool:
    """A doorbell that reports "not rung" — the dev shape, where no transport exists."""
    return False


class TestTheNightlyClock:
    """The scheduler (ADR 0018): who it wakes, what it enqueues, and what it cannot see.

    The clock is deliberately the smallest thing that could work — it holds no provider
    credential and fetches nothing, so the only harm it can do is enqueue the wrong work
    or enqueue it twice. Both are what these assert against.
    """

    async def _schedule(self, org_id: uuid.UUID, *, timezone: str, hour: int) -> None:
        async with org_session(org_id) as session:
            await session.execute(
                text("SELECT sched.write_schedule(:tz, CAST(:h AS smallint), true, NULL)"),
                {"tz": timezone, "h": hour},
            )

    async def _sync_keys(self, org_id: uuid.UUID) -> list[str]:
        async with org_session(org_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT idempotency_key FROM jobs WHERE kind = 'connector.sync' "
                            "ORDER BY idempotency_key"
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [str(row) for row in rows]

    async def test_a_due_organisation_gets_one_sync_per_connected_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jutsu_worker import schedule

        org_id = uuid.uuid4()
        connection_id, _ = await seed_connection_and_job(org_id)
        await self._schedule(org_id, timezone="UTC", hour=1)

        rung: list[uuid.UUID] = []

        async def fake_ring(target: uuid.UUID) -> bool:
            rung.append(target)
            return True

        monkeypatch.setattr(schedule, "_ring", fake_ring)

        run = await schedule.sync_organisation(
            schedule.DueOrganisation(org_id=org_id, timezone="UTC", hour_local=1)
        )

        assert run is not None
        assert run.connections == 1
        assert run.outcome == "success"
        assert rung == [org_id], "the worker must be woken, or the rows just sit there"
        assert f"connector.sync:{org_id}:{connection_id}" in await self._sync_keys(org_id)

    async def test_it_shares_the_idempotency_key_with_sync_now(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A nightly run and a person pressing Sync now a second earlier must not produce
        two walks of one connection. The shared key is the only thing preventing it."""
        from jutsu_api.connectors import sync_now
        from jutsu_worker import schedule

        org_id = uuid.uuid4()
        connection_id, _ = await seed_connection_and_job(org_id)
        await self._schedule(org_id, timezone="UTC", hour=1)
        monkeypatch.setattr(schedule, "_ring", _not_rung)

        async with org_session(org_id) as session:
            user_id = (
                await session.execute(
                    text("SELECT user_id FROM connections WHERE id = :c"), {"c": connection_id}
                )
            ).scalar_one()

        await schedule.sync_organisation(
            schedule.DueOrganisation(org_id=org_id, timezone="UTC", hour_local=1)
        )
        async with org_session(org_id) as session:
            await sync_now(session, org_id=org_id, user_id=user_id, connection_id=connection_id)

        # Exactly one row carries the canonical key, though both paths wrote it. (The
        # fixture also seeds a row under the pre-org-qualified key this suite predates;
        # it is not what either production path writes, so it is not counted.)
        keys = await self._sync_keys(org_id)
        assert keys.count(f"connector.sync:{org_id}:{connection_id}") == 1, keys

    async def test_a_second_tick_the_same_day_claims_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tick runs every fifteen minutes and an organisation is due for a whole
        hour; without the claim the run would start four times over."""
        from jutsu_worker import schedule

        org_id = uuid.uuid4()
        await seed_connection_and_job(org_id)
        await self._schedule(org_id, timezone="UTC", hour=1)
        monkeypatch.setattr(schedule, "_ring", _not_rung)

        due = schedule.DueOrganisation(org_id=org_id, timezone="UTC", hour_local=1)
        first = await schedule.sync_organisation(due)
        second = await schedule.sync_organisation(due)

        assert first is not None
        assert second is None

    async def test_it_only_touches_the_organisation_it_was_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The org id is a hint about where to look, never an authorization: every write
        happens inside `org_session`, so another tenant is invisible to it."""
        from jutsu_worker import schedule

        org_a, org_b = uuid.uuid4(), uuid.uuid4()
        await seed_connection_and_job(org_a)
        await seed_connection_and_job(org_b)
        await self._schedule(org_a, timezone="UTC", hour=1)
        monkeypatch.setattr(schedule, "_ring", _not_rung)

        await schedule.sync_organisation(
            schedule.DueOrganisation(org_id=org_a, timezone="UTC", hour_local=1)
        )

        assert len(await self._sync_keys(org_b)) == 1, "org B keeps only its own seeded row"

    async def test_the_run_is_recorded_where_an_administrator_can_read_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jutsu_worker import schedule

        org_id = uuid.uuid4()
        await seed_connection_and_job(org_id)
        await self._schedule(org_id, timezone="UTC", hour=1)
        monkeypatch.setattr(schedule, "_ring", _not_rung)

        await schedule.sync_organisation(
            schedule.DueOrganisation(org_id=org_id, timezone="UTC", hour_local=1)
        )

        async with org_session(org_id) as session:
            row = (await session.execute(text("SELECT * FROM sched.read_schedule()"))).one()
        assert row.last_outcome == "success"
        assert row.last_connections == 1
        assert row.last_finished_at is not None

    async def test_a_tick_runs_only_organisations_whose_local_hour_has_arrived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from jutsu_worker import schedule

        indian, utc_org = uuid.uuid4(), uuid.uuid4()
        await seed_connection_and_job(indian)
        await seed_connection_and_job(utc_org)
        await self._schedule(indian, timezone="Asia/Kolkata", hour=1)
        await self._schedule(utc_org, timezone="UTC", hour=1)
        monkeypatch.setattr(schedule, "_ring", _not_rung)

        # 01:30 in Kolkata is 20:00 the previous day in UTC: the Indian organisation is
        # due and the UTC one is not, which is the entire point of storing a zone.
        runs = await schedule.run_due_syncs(
            now=datetime(2026, 9, 8, 1, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
        )

        assert [run.org_id for run in runs] == [indian]
