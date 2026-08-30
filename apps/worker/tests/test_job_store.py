"""The durable job store, against a real Postgres with the real policies.

Every claim here is about the database — row-level security, `FOR UPDATE SKIP LOCKED`,
a unique constraint, a lease expiring — and none of it has an in-memory equivalent. A
mocked session would pass against a store that leaks across tenants and duplicates work,
which is the only kind of green worth being afraid of.

Two fixtures, deliberately different:

  * `session` is the **restricted** `jutsu_app` role, which is `NOSUPERUSER NOBYPASSRLS`.
    Everything that exercises the store runs through it, because a superuser bypasses RLS
    unconditionally and would make every isolation assertion below vacuous (ADR 0003).
  * `inspector` is the owner, used only to *look*. Assertions of the form "no row was
    created" are meaningless on an RLS-scoped session, which returns nothing whether or
    not the row exists.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from jutsu_worker.jobs import (
    DEFAULT_MAX_ATTEMPTS,
    FailureKind,
    JobKind,
    JobState,
    backoff_delay,
    claim_job,
    complete_job,
    enqueue_job,
    fail_job,
    reclaim_expired_leases,
    record_state,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"


def skip_without_database() -> None:
    if os.environ.get("JUTSU_DB_REACHABLE") != "1":
        pytest.skip(f"nothing listening at {TEST_DB_ENV} — start Postgres with `make up`")


def alembic_config(url: str) -> Config:
    root = Path(__file__).resolve().parents[3] / "packages" / "db"
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src" / "jutsu_db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def run_alembic(cfg: Config, direction: str, revision: str) -> None:
    """Alembic's env.py ends in `asyncio.run`, which cannot nest in a running loop."""
    fn = command.upgrade if direction == "upgrade" else command.downgrade
    await asyncio.to_thread(fn, cfg, revision)


@pytest.fixture
async def migrated(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[str, str]]:
    """A freshly migrated schema. Yields `(app_url, migration_url)`.

    Fixtures live in the test module rather than a `conftest.py` because only one
    `conftest.py` may exist under `apps/` — a second one collides and makes `mypy apps`
    check nothing at all, which the Makefile records.
    """
    skip_without_database()
    app_url = os.environ[TEST_DB_ENV]
    migration_url = os.environ.get(MIGRATION_DB_ENV, app_url)

    monkeypatch.setenv("DATABASE_URL", migration_url)
    cfg = alembic_config(migration_url)
    await run_alembic(cfg, "downgrade", "base")
    await run_alembic(cfg, "upgrade", "head")

    yield app_url, migration_url

    await run_alembic(cfg, "downgrade", "base")


@pytest.fixture
async def sessions(
    migrated: tuple[str, str],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A factory for **independent** transactions against the same migrated schema.

    `session` yields one transaction, which is the right shape for nearly everything in
    this file. Lease safety is not one of them: it is a property *between* transactions,
    and a session cannot observe a row lock that it is itself holding.
    """
    engine = create_async_engine(migrated[0])
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


@pytest.fixture
async def session(migrated: tuple[str, str]) -> AsyncIterator[AsyncSession]:
    """One transaction as the restricted application role."""
    engine = create_async_engine(migrated[0])
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as opened, opened.begin():
        yield opened
        await opened.rollback()
    await engine.dispose()


async def scope(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
    )


async def make_org(session: AsyncSession, label: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    await scope(session, org_id)
    await session.execute(
        text("INSERT INTO orgs (id, name) VALUES (:i, :n)"), {"i": org_id, "n": label}
    )
    return org_id


async def state_of(session: AsyncSession, job_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            text(
                "SELECT state, attempts, failure_kind, error, locked_until, next_attempt_at "
                "FROM jobs WHERE id = :i"
            ),
            {"i": str(job_id)},
        )
    ).one()


async def expire_lease(session: AsyncSession, job_id: uuid.UUID) -> None:
    """Simulate a crashed worker: the row stays claimed, the lease stops being renewed.

    Nothing needs to *detect* the crash. A killed process runs no cleanup, so the only
    evidence it ever leaves is a lease that stops moving, and that is exactly what this
    reproduces.
    """
    await session.execute(
        text("UPDATE jobs SET locked_until = now() - INTERVAL '1 second' WHERE id = :i"),
        {"i": str(job_id)},
    )


class TestIdempotentEnqueue:
    async def test_a_job_is_created_once(self, session: AsyncSession) -> None:
        org = await make_org(session, "alpha")

        first = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        second = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )

        assert first is not None
        assert second is None, "a duplicate key must not create a second job"
        count = (await session.execute(text("SELECT count(*) FROM jobs"))).scalar_one()
        assert count == 1

    async def test_the_duplicate_is_refused_by_the_database(self, session: AsyncSession) -> None:
        """Not a check-then-insert.

        Two workers can ask at the same instant, and a read followed by a write would let
        both see "absent" and both insert. The unique constraint is what makes the race
        unrepresentable rather than unlikely.
        """
        definition = (
            await session.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'uq_jobs_idempotency_key'"
                )
            )
        ).scalar_one()

        assert "UNIQUE" in definition

    async def test_different_keys_create_different_jobs(self, session: AsyncSession) -> None:
        org = await make_org(session, "alpha")

        a = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="a"
        )
        b = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="b"
        )

        assert a is not None and b is not None and a != b

    async def test_the_payload_round_trips(self, session: AsyncSession) -> None:
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session,
            org_id=org,
            kind=JobKind.INGEST_DOCUMENT,
            idempotency_key="k",
            payload={"external_id": "mail/1", "source_id": str(uuid.uuid4())},
        )
        assert job_id is not None

        claimed = await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=job_id)

        assert claimed is not None
        assert claimed.payload["external_id"] == "mail/1"


class TestTenantIsolation:
    async def test_another_tenants_job_cannot_be_claimed(self, session: AsyncSession) -> None:
        """The dispatch message carries an org id, so this is the attack it enables.

        A forged message naming org B and a job id from org A must reach nothing. Under
        the policy the row is simply invisible, so the claim finds no work — the failure
        is a wasted round trip, never a cross-tenant read.
        """
        alpha = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=alpha, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None

        # Scope moves to Beta; Alpha's job id is still known, as it would be to an
        # attacker replaying a dispatch message.
        await make_org(session, "beta")

        claimed = await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=job_id)

        assert claimed is None, "a job was claimed from inside another tenant's scope"

    async def test_another_tenants_job_is_invisible(self, session: AsyncSession) -> None:
        alpha = await make_org(session, "alpha")
        await enqueue_job(session, org_id=alpha, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k")

        await make_org(session, "beta")

        count = (await session.execute(text("SELECT count(*) FROM jobs"))).scalar_one()
        assert count == 0, "a count leaked another tenant's queue depth"

    async def test_an_unscoped_session_sees_no_jobs(self, session: AsyncSession) -> None:
        """Fail-closed: forgetting to scope yields an empty queue, never every queue."""
        alpha = await make_org(session, "alpha")
        await enqueue_job(session, org_id=alpha, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k")

        await session.execute(text("SELECT set_config('app.current_org_id', '', true)"))

        assert (await session.execute(text("SELECT count(*) FROM jobs"))).scalar_one() == 0
        assert await claim_job(session, kind=JobKind.INGEST_DOCUMENT) is None

    async def test_enqueueing_into_another_tenant_is_refused(self, session: AsyncSession) -> None:
        """`WITH CHECK` stops the write, so a wrong org id is an error rather than a leak."""
        await make_org(session, "alpha")
        foreign = uuid.uuid4()

        with pytest.raises(Exception):  # noqa: B017 - DBAPIError from the policy
            await enqueue_job(
                session, org_id=foreign, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
            )

    async def test_reclaim_only_touches_the_scoped_tenant(self, session: AsyncSession) -> None:
        """The org-scoped sweep must not reach across tenants even by accident.

        Both tenants have a crashed job. Sweeping from Alpha's scope must reclaim exactly
        one — and the count alone would not prove it, since reclaiming Beta's and leaving
        Alpha's also returns 1. Each job is therefore read back under its **own** scope,
        which is the only way to see it at all.
        """
        alpha = await make_org(session, "alpha")
        alpha_job = await enqueue_job(
            session, org_id=alpha, kind=JobKind.INGEST_DOCUMENT, idempotency_key="a"
        )
        assert alpha_job is not None
        await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=alpha_job)
        await expire_lease(session, alpha_job)

        beta = await make_org(session, "beta")
        beta_job = await enqueue_job(
            session, org_id=beta, kind=JobKind.INGEST_DOCUMENT, idempotency_key="b"
        )
        assert beta_job is not None
        await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=beta_job)
        await expire_lease(session, beta_job)

        await scope(session, alpha)
        reclaimed = await reclaim_expired_leases(session)

        assert reclaimed == 1
        assert (await state_of(session, alpha_job)).state == JobState.RETRY_SCHEDULED.value

        await scope(session, beta)
        assert (await state_of(session, beta_job)).state == JobState.FETCHING.value, (
            "Beta's job was swept from Alpha's scope"
        )


class TestClaiming:
    async def test_a_claim_takes_the_lease_and_counts_the_attempt(
        self, session: AsyncSession
    ) -> None:
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None

        claimed = await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=job_id)

        assert claimed is not None
        assert claimed.attempts == 1
        row = await state_of(session, job_id)
        assert row.state == JobState.FETCHING.value
        assert row.locked_until is not None, "a claim without a lease is unrecoverable"

    async def test_a_claimed_job_cannot_be_claimed_again(self, session: AsyncSession) -> None:
        """The lease, not the transaction, is what excludes the second worker.

        `FOR UPDATE SKIP LOCKED` only excludes concurrent transactions. Once the first
        claim commits, what keeps a second worker off the row is `locked_until` still
        being in the future.
        """
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None

        first = await claim_job(session, kind=JobKind.INGEST_DOCUMENT)
        second = await claim_job(session, kind=JobKind.INGEST_DOCUMENT)

        assert first is not None
        assert second is None

    async def test_a_job_of_another_kind_is_not_claimed(self, session: AsyncSession) -> None:
        """The three stages are separate queues sharing a table."""
        org = await make_org(session, "alpha")
        await enqueue_job(session, org_id=org, kind=JobKind.EMBED_DOCUMENT, idempotency_key="k")

        assert await claim_job(session, kind=JobKind.INGEST_DOCUMENT) is None
        assert await claim_job(session, kind=JobKind.EMBED_DOCUMENT) is not None

    async def test_a_scheduled_retry_is_not_claimed_early(self, session: AsyncSession) -> None:
        """`next_attempt_at` is the backoff, so claiming must honour it."""
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None
        await session.execute(
            text(
                "UPDATE jobs SET state = :s, next_attempt_at = now() + INTERVAL '1 hour', "
                "locked_until = NULL WHERE id = :i"
            ),
            {"s": JobState.RETRY_SCHEDULED.value, "i": str(job_id)},
        )

        assert await claim_job(session, kind=JobKind.INGEST_DOCUMENT) is None

        await session.execute(
            text("UPDATE jobs SET next_attempt_at = now() - INTERVAL '1 second' WHERE id = :i"),
            {"i": str(job_id)},
        )
        assert await claim_job(session, kind=JobKind.INGEST_DOCUMENT) is not None

    async def test_terminal_jobs_are_never_reclaimed(self, session: AsyncSession) -> None:
        org = await make_org(session, "alpha")
        for index, state in enumerate((JobState.COMPLETED, JobState.FAILED, JobState.DEAD_LETTER)):
            job_id = await enqueue_job(
                session,
                org_id=org,
                kind=JobKind.INGEST_DOCUMENT,
                idempotency_key=f"k{index}",
            )
            assert job_id is not None
            await session.execute(
                text("UPDATE jobs SET state = :s, locked_until = NULL WHERE id = :i"),
                {"s": state.value, "i": str(job_id)},
            )

        assert await claim_job(session, kind=JobKind.INGEST_DOCUMENT) is None
        assert await reclaim_expired_leases(session) == 0


class TestCrashRecovery:
    async def test_an_expired_lease_is_reclaimed(self, session: AsyncSession) -> None:
        """The crash case. A killed worker runs no cleanup; the lease expiring is the signal."""
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None
        await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=job_id)
        await expire_lease(session, job_id)

        reclaimed = await reclaim_expired_leases(session)

        assert reclaimed == 1
        row = await state_of(session, job_id)
        assert row.state == JobState.RETRY_SCHEDULED.value
        assert row.locked_until is None
        assert row.next_attempt_at is None, "a reclaimed job should be runnable at once"

    async def test_a_live_lease_is_left_alone(self, session: AsyncSession) -> None:
        """A slow document must not be reclaimed out from under the worker running it."""
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None
        await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=job_id)

        assert await reclaim_expired_leases(session) == 0
        assert (await state_of(session, job_id)).state == JobState.FETCHING.value

    async def test_a_reclaimed_job_is_claimable_again(self, session: AsyncSession) -> None:
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None
        await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=job_id)
        await expire_lease(session, job_id)
        await reclaim_expired_leases(session)

        again = await claim_job(session, kind=JobKind.INGEST_DOCUMENT)

        assert again is not None
        assert again.id == job_id
        assert again.attempts == 2, "the reclaimed attempt must still count toward the bound"

    async def test_crash_looping_still_reaches_dead_letter(self, session: AsyncSession) -> None:
        """Reclamation must not reset the ladder, or a crash loop never terminates.

        This is the property that makes lease recovery safe: an attempt is counted when
        the job is *claimed*, so a worker that dies every time still exhausts its bounded
        attempts instead of being retried for ever.
        """
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None

        attempts = 0
        for _ in range(DEFAULT_MAX_ATTEMPTS + 2):
            claimed = await claim_job(session, kind=JobKind.INGEST_DOCUMENT)
            if claimed is None:
                break
            attempts = claimed.attempts
            await expire_lease(session, job_id)
            await reclaim_expired_leases(session)

        assert attempts >= DEFAULT_MAX_ATTEMPTS, "attempts were reset by reclamation"


class TestFailureClassification:
    async def test_a_transient_failure_schedules_a_retry(self, session: AsyncSession) -> None:
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.EMBED_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None
        claimed = await claim_job(session, kind=JobKind.EMBED_DOCUMENT)
        assert claimed is not None

        state = await fail_job(
            session,
            job_id=job_id,
            failure_kind=FailureKind.EMBEDDING_TRANSIENT,
            message="429 from provider",
            attempts=claimed.attempts,
            retryable=True,
        )

        assert state is JobState.RETRY_SCHEDULED
        row = await state_of(session, job_id)
        assert row.failure_kind == FailureKind.EMBEDDING_TRANSIENT.value
        assert row.next_attempt_at is not None
        assert row.locked_until is None, "a failed job must release its lease"

    async def test_a_permanent_failure_does_not_retry(self, session: AsyncSession) -> None:
        """A malformed document and a 400 are rejected identically every time."""
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None
        claimed = await claim_job(session, kind=JobKind.INGEST_DOCUMENT)
        assert claimed is not None

        state = await fail_job(
            session,
            job_id=job_id,
            failure_kind=FailureKind.MALFORMED_DOCUMENT,
            message="unparsable message",
            attempts=claimed.attempts,
            retryable=False,
        )

        assert state is JobState.FAILED
        row = await state_of(session, job_id)
        assert row.next_attempt_at is None
        assert await claim_job(session, kind=JobKind.INGEST_DOCUMENT) is None

    async def test_exhausted_attempts_dead_letter(self, session: AsyncSession) -> None:
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.EMBED_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None

        state = await fail_job(
            session,
            job_id=job_id,
            failure_kind=FailureKind.EMBEDDING_TRANSIENT,
            message="still failing",
            attempts=DEFAULT_MAX_ATTEMPTS,
            retryable=True,
        )

        assert state is JobState.DEAD_LETTER
        assert await claim_job(session, kind=JobKind.EMBED_DOCUMENT) is None
        row = await state_of(session, job_id)
        assert row.failure_kind == FailureKind.EMBEDDING_TRANSIENT.value, (
            "dead-lettering must keep why it died"
        )

    async def test_every_failure_kind_is_distinguishable(self, session: AsyncSession) -> None:
        """The point of the enum: one FAILED state loses what an operator needs."""
        org = await make_org(session, "alpha")
        for index, kind in enumerate(FailureKind):
            job_id = await enqueue_job(
                session,
                org_id=org,
                kind=JobKind.INGEST_DOCUMENT,
                idempotency_key=f"k{index}",
            )
            assert job_id is not None
            await fail_job(
                session,
                job_id=job_id,
                failure_kind=kind,
                message="x",
                attempts=1,
                retryable=False,
            )
            assert (await state_of(session, job_id)).failure_kind == kind.value

    async def test_success_clears_the_failure_fields(self, session: AsyncSession) -> None:
        """A job that succeeded on its third attempt must not read as still failing."""
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.EMBED_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None
        await fail_job(
            session,
            job_id=job_id,
            failure_kind=FailureKind.EMBEDDING_TRANSIENT,
            message="429",
            attempts=1,
            retryable=True,
        )

        await complete_job(session, job_id=job_id)

        row = await state_of(session, job_id)
        assert row.state == JobState.COMPLETED.value
        assert row.failure_kind is None
        assert row.error is None
        assert row.next_attempt_at is None


class TestBackoff:
    def test_it_grows_and_is_bounded(self) -> None:
        # S311: seeded precisely so the schedule is reproducible. A CSPRNG here would
        # make the assertion untestable and buy nothing — this is jitter, not a secret.
        rng = random.Random(1)  # noqa: S311
        ceilings = [max(backoff_delay(n, rng=rng) for _ in range(200)) for n in range(1, 8)]

        assert ceilings[0] < ceilings[2] < ceilings[4], "backoff does not grow"
        assert all(delay <= 300.0 for delay in ceilings), "backoff is unbounded"

    def test_it_is_jittered(self) -> None:
        """Full jitter, not a fixed schedule.

        When a per-minute quota rejects several jobs at once, an unjittered backoff
        retries them all at the same instant and they fail together again.
        """
        rng = random.Random(7)  # noqa: S311
        samples = {backoff_delay(4, rng=rng) for _ in range(50)}

        assert len(samples) > 40, "delays are not jittered"
        assert min(samples) >= 0.0


class TestStateTransitions:
    async def test_progress_states_are_recorded(self, session: AsyncSession) -> None:
        """A stuck job must say *where* it stuck, not only that it did."""
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None

        for state in (
            JobState.NORMALIZED,
            JobState.ACL_CAPTURED,
            JobState.MASKED,
            JobState.CHUNKED,
        ):
            await record_state(session, job_id=job_id, state=state)
            assert (await state_of(session, job_id)).state == state.value

    async def test_recording_can_extend_the_lease(self, session: AsyncSession) -> None:
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None
        await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=job_id)
        before = (await state_of(session, job_id)).locked_until

        await record_state(session, job_id=job_id, state=JobState.MASKED, extend_lease_by=900)

        after = (await state_of(session, job_id)).locked_until
        assert after > before

    async def test_recording_without_a_lease_argument_leaves_it_alone(
        self, session: AsyncSession
    ) -> None:
        org = await make_org(session, "alpha")
        job_id = await enqueue_job(
            session, org_id=org, kind=JobKind.INGEST_DOCUMENT, idempotency_key="k"
        )
        assert job_id is not None
        await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=job_id)
        before = (await state_of(session, job_id)).locked_until

        await record_state(session, job_id=job_id, state=JobState.MASKED)

        assert (await state_of(session, job_id)).locked_until == before


class TestALiveWorkerKeepsItsJob:
    """The lease recovers **crashed** workers, and must not touch running ones.

    This exists because of a defect that was reported and turned out not to be real. A
    `Retry-After` from the embedding provider can legitimately park a job for minutes,
    and the arithmetic looks alarming: five attempts, each able to wait up to
    `MAX_RETRY_AFTER_S` (120s), against a `DEFAULT_LEASE_SECONDS` of 300. On paper the
    lease expires while the worker is still healthily waiting, another worker reclaims
    the row, and the document is embedded twice at full price.

    It cannot happen, and the reason is not the lease. `run_embedding_job` calls
    `record_state` **before** the provider call and inside the work transaction, so the
    job row carries a row-level write lock for the whole duration of the work. A
    reclaiming `UPDATE` must take that same lock and cannot; a claiming
    `SELECT ... FOR UPDATE SKIP LOCKED` steps over the row entirely. The lease only
    becomes reachable once the transaction is gone, which is exactly what a dead worker
    leaves behind.

    So these tests pin an invariant that already holds and that nothing states, because
    the obvious "improvement" - committing the state transition early so that a network
    call is not made while holding a lock - would silently make the original defect real.
    `test_the_protection_is_the_row_lock` is the one that would go red.

    Deterministic on purpose: the lease is expired by committing a past `locked_until`
    rather than by sleeping, so nothing here waits on a clock.
    """

    async def _claimed_job(
        self, factory: async_sessionmaker[AsyncSession], label: str
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """An org with one claimed `embed.document` job whose lease has already expired.

        Every step commits, because each assertion below is about what a *different*
        transaction can see and do.
        """
        async with factory() as opened, opened.begin():
            org = await make_org(opened, label)
            await enqueue_job(
                opened,
                org_id=org,
                kind=JobKind.EMBED_DOCUMENT,
                idempotency_key=f"embed-{label}",
                payload={"document_id": str(uuid.uuid4())},
            )

        async with factory() as opened, opened.begin():
            await scope(opened, org)
            job = await claim_job(opened, kind=JobKind.EMBED_DOCUMENT)
        assert job is not None

        async with factory() as opened, opened.begin():
            await scope(opened, org)
            await expire_lease(opened, job.id)

        return org, job.id

    async def test_a_reclaimer_cannot_take_a_row_a_live_worker_holds(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        org, job_id = await self._claimed_job(sessions, "live")

        async with sessions() as worker, worker.begin():
            await scope(worker, org)
            # Exactly what `run_embedding_job` does before it calls the provider.
            await record_state(worker, job_id=job_id, state=JobState.EMBEDDING)

            async with sessions() as reaper, reaper.begin():
                await scope(reaper, org)
                # Without this the reclaim blocks until the worker commits and the test
                # hangs instead of failing. The timeout makes "blocked" observable.
                await reaper.execute(text("SET LOCAL lock_timeout = '2s'"))
                with pytest.raises(Exception, match="LockNotAvailable"):
                    await reclaim_expired_leases(reaper, kinds=[JobKind.EMBED_DOCUMENT])

        async with sessions() as check, check.begin():
            await scope(check, org)
            row = await state_of(check, job_id)
        assert row.state == JobState.EMBEDDING.value, "a live job was moved out from under it"
        assert row.attempts == 1, "a live job was charged a second attempt"

    async def test_a_second_worker_does_not_claim_a_live_job(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """`SKIP LOCKED` steps over the row rather than waiting for it.

        This is the half that prevents duplicate provider spend: a blocked reclaimer is
        merely slow, but a second *claim* would embed the same document again at cost.
        """
        org, job_id = await self._claimed_job(sessions, "dup")

        async with sessions() as worker, worker.begin():
            await scope(worker, org)
            await record_state(worker, job_id=job_id, state=JobState.EMBEDDING)

            async with sessions() as other, other.begin():
                await scope(other, org)
                await other.execute(text("SET LOCAL lock_timeout = '2s'"))
                assert await claim_job(other, kind=JobKind.EMBED_DOCUMENT) is None

    async def test_the_lease_still_recovers_a_crashed_worker(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The protection must not have cost us stale-job recovery.

        A killed process leaves no transaction, so the lock is gone and the expired lease
        is reachable again - which is the whole design.
        """
        org, job_id = await self._claimed_job(sessions, "crash")

        worker = sessions()
        await worker.__aenter__()
        transaction = await worker.begin().__aenter__()
        await scope(worker, org)
        await record_state(worker, job_id=job_id, state=JobState.EMBEDDING)
        # SIGKILL leaves the server to roll the transaction back. This is that rollback.
        await transaction.rollback()
        await worker.__aexit__(None, None, None)

        async with sessions() as reaper, reaper.begin():
            await scope(reaper, org)
            assert await reclaim_expired_leases(reaper, kinds=[JobKind.EMBED_DOCUMENT]) == 1

        async with sessions() as check, check.begin():
            await scope(check, org)
            row = await state_of(check, job_id)
        assert row.state == JobState.RETRY_SCHEDULED.value
        assert row.attempts == 1, "recovery must not re-charge the attempt already counted"

    async def test_the_protection_is_the_row_lock(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Name what the invariant actually rests on.

        An open transaction is not enough - the row has to have been *written*. A work
        transaction that only read would leave the lease reachable, so moving
        `record_state` after the provider call, or committing it early, reopens the
        duplicate-spend hole this class exists to describe.
        """
        org, job_id = await self._claimed_job(sessions, "readonly")

        async with sessions() as worker, worker.begin():
            await scope(worker, org)
            await state_of(worker, job_id)  # a read, and nothing else

            async with sessions() as reaper, reaper.begin():
                await scope(reaper, org)
                await reaper.execute(text("SET LOCAL lock_timeout = '2s'"))
                reclaimed = await reclaim_expired_leases(reaper, kinds=[JobKind.EMBED_DOCUMENT])

        assert reclaimed == 1, (
            "a read-only work transaction did not lose the row - the protection proven "
            "in the tests above may be coming from something other than the row lock"
        )
