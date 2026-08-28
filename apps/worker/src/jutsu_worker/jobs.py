"""The durable job store. Postgres is the source of truth; Redis is a doorbell.

That split is the whole reliability story, so it is worth stating before anything else:
**a job exists because a row exists.** arq only tells a worker that there may be work; if
Redis is flushed, restarted, or never delivers the message at all, no durable work is
lost, because the row is still there and the next source run finds it. Nothing here reads
job state from Redis and nothing writes authority to it.

**Every read and write is org-scoped, and the scope never comes from the payload.**
Migration 0010 put `jobs` under row-level security, so a session that has not set
`app.current_org_id` sees no jobs at all, and a session scoped to the wrong tenant sees
none of another's. A dispatch message carrying a forged `org_id` therefore cannot reach
another tenant's row — it can only fail to find its own, which is a no-op. That is the
property that makes it safe for the queue to carry an org id at all.

**Claiming is a lease, not a lock.** `FOR UPDATE SKIP LOCKED` picks a row no other worker
is touching *right now*; `locked_until` is what survives a worker being killed. A process
that dies holding a claim leaves a row whose lease simply expires, and the next sweep
reclaims it. There is no cleanup path to forget to run, because the absence of a heartbeat
*is* the signal.

**There is no cross-tenant sweeper, deliberately.** Reclaiming expired leases requires
reading `jobs`, and under `FORCE ROW LEVEL SECURITY` nothing can read it across tenants —
not the app role, not the table owner, and not a `SECURITY DEFINER` function, which
returns zero rows with no error (verified, and recorded in ADR 0012). So reclamation is
org-scoped and runs at the start of each source run for that org. The consequence is
stated rather than hidden: **an organisation whose source is never processed again keeps
its orphaned jobs.** A multi-tenant scheduler is what closes that, and it is a later slice
with its own tenant-enumeration decision to make.
"""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "FailureKind",
    "Job",
    "JobKind",
    "JobState",
    "backoff_delay",
    "claim_job",
    "complete_job",
    "enqueue_job",
    "fail_job",
    "reclaim_expired_leases",
    "record_state",
    "reopen_completed_job",
]


class JobKind(StrEnum):
    """The three durable stages. §9 requires that a failure at one never forces another.

    `INGEST_DOCUMENT` and `EMBED_DOCUMENT` are separate rows on purpose, not two phases of
    one row: embedding is the stage that costs money and the stage most likely to fail
    transiently, and re-running it must never re-fetch from the source.
    """

    INGEST_SOURCE = "ingest.source"
    INGEST_DOCUMENT = "ingest.document"
    EMBED_DOCUMENT = "embed.document"


class JobState(StrEnum):
    """Progress, then terminal.

    The progress states are checkpoints in the per-document pipeline and exist so a stuck
    job says *where* it stuck. They are recorded as the stage completes, so `MASKED` means
    masking finished and chunking has not.

    `CHUNKED` is where document, ACL and chunk rows are written — they must exist before
    anything can embed them — and `PERSISTED` is where the vectors land. The sequence is
    §9's, and that is the one place the two readings could differ.
    """

    PENDING = "pending"
    FETCHING = "fetching"
    NORMALIZED = "normalized"
    ACL_CAPTURED = "acl_captured"
    MASKED = "masked"
    CHUNKED = "chunked"
    EMBEDDING = "embedding"
    PERSISTED = "persisted"
    COMPLETED = "completed"

    #: Transient failure, backoff scheduled. `next_attempt_at` says when.
    RETRY_SCHEDULED = "retry_scheduled"
    #: Permanent failure. Retrying cannot help, so nothing will retry it.
    FAILED = "failed"
    #: Bounded attempts exhausted. Needs a human, and says so rather than looping.
    DEAD_LETTER = "dead_letter"


#: States a worker may claim. Everything else is either in flight or finished.
CLAIMABLE: Final = (JobState.PENDING, JobState.RETRY_SCHEDULED)

#: States that mean the job is over, whatever the outcome.
TERMINAL: Final = (JobState.COMPLETED, JobState.FAILED, JobState.DEAD_LETTER)


class FailureKind(StrEnum):
    """Why it failed, which is the half a single FAILED state throws away.

    An operator's first question is never "did it fail" — the state says that. It is
    "can this be retried, and if not, what has to change first". These are the distinct
    answers, and each maps to a different action.
    """

    #: The connector could not reach or read the source. Retry may help.
    SOURCE_UNAVAILABLE = "source_unavailable"
    #: The document exists and will never parse. Retrying is pointless; the corpus is
    #: what has to change.
    MALFORMED_DOCUMENT = "malformed_document"
    #: A 429, a 5xx, a timeout. The provider may well succeed next time.
    EMBEDDING_TRANSIENT = "embedding_transient"
    #: A 4xx that is not 429, or a silently truncated input. Rejected identically every
    #: time, so retrying spends real quota to be told so again.
    EMBEDDING_PERMANENT = "embedding_permanent"
    #: The run's token ceiling was reached. Not an error in the source — a budget
    #: decision — so it is held rather than failed.
    BUDGET_EXHAUSTED = "budget_exhausted"
    #: A bug here rather than out there.
    INTERNAL = "internal"


#: How long a claim is held before any worker may take it back. Long enough that a slow
#: document does not lose its own lease, short enough that a killed worker's jobs do not
#: sit idle for an operator-visible time.
DEFAULT_LEASE_SECONDS: Final = 300

#: Bounded, and low. A job that has failed transiently five times is not going to succeed
#: on the sixth; it is going to keep a worker busy being wrong.
DEFAULT_MAX_ATTEMPTS: Final = 5

#: Backoff bounds. The ceiling matters more than the base: without one, attempt five of an
#: exponential schedule parks a job for hours.
BASE_BACKOFF_SECONDS: Final = 2.0
MAX_BACKOFF_SECONDS: Final = 300.0


@dataclass(frozen=True, slots=True)
class Job:
    """One claimed row, as the worker sees it."""

    id: uuid.UUID
    org_id: uuid.UUID
    kind: JobKind
    state: JobState
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int

    @property
    def correlation_id(self) -> str:
        """The identifier that ties a job's audit rows and log lines together.

        Derived from the job id rather than generated, so it is stable across every
        attempt, every worker and every restart — a random one per run would make the
        audit trail of a retried job impossible to follow.
        """
        return str(self.id)


def backoff_delay(attempts: int, *, rng: random.Random | None = None) -> float:
    """Exponential backoff with **full** jitter, in seconds.

    Full jitter rather than a fixed schedule, for the reason the embedding client already
    records: when a per-minute quota rejects several jobs at once, an unjittered backoff
    retries them all at the same instant and they fail together again. Sampling uniformly
    from `[0, ceiling]` spreads them instead.
    """
    source = rng or random
    ceiling = min(BASE_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0)), MAX_BACKOFF_SECONDS)
    # S311: jitter, not cryptography. A CSPRNG here would buy nothing and make the
    # schedule untestable.
    return source.uniform(0.0, ceiling)


async def enqueue_job(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    kind: JobKind,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID | None:
    """Create a job if that key has never been seen. Returns its id, or None if it existed.

    `ON CONFLICT DO NOTHING` on `idempotency_key` is the whole deduplication mechanism, and
    it is a **database** constraint rather than a check-then-insert, so two workers racing
    on the same document produce one row rather than two.

    Returning `None` for a duplicate is deliberate: the caller learns that this exact work
    was already queued, which is what makes a repeated source run — or a repeated
    `make seed` — add nothing.

    The org id is written from the argument and the row is then only ever readable inside
    that org's scope, because `WITH CHECK` on the policy refuses an insert attributed to
    any other tenant. A caller that passes a foreign org id does not write a foreign row;
    it gets an error.
    """
    row = (
        await session.execute(
            text(
                "INSERT INTO jobs (id, org_id, kind, state, idempotency_key, payload_json) "
                "VALUES (:id, :org, :kind, :state, :key, CAST(:payload AS jsonb)) "
                "ON CONFLICT (idempotency_key) DO NOTHING "
                "RETURNING id"
            ),
            {
                "id": str(uuid.uuid4()),
                "org": str(org_id),
                "kind": kind.value,
                "state": JobState.PENDING.value,
                "key": idempotency_key,
                "payload": json.dumps(payload or {}),
            },
        )
    ).first()
    return uuid.UUID(str(row.id)) if row is not None else None


async def reopen_completed_job(session: AsyncSession, *, idempotency_key: str) -> bool:
    """Make a finished job runnable again. Returns whether it did anything.

    **This is what makes a re-sync notice a changed document.** The job key is the document
    *identity*, not its version, so a second source walk finds the same key and enqueues
    nothing — and without this the document would never be looked at again, however often
    its content changed. That was a real defect, caught by seeding twice and finding the
    second run did no work at all.

    Only `completed` is reopened, and the exclusions are the point:

      * **in flight** — a lease is held, or the job is queued and waiting. Resetting it
        would let two workers run the same document.
      * **failed / dead_letter** — a permanent failure reopened by every sync becomes an
        infinite loop that never reaches anybody's attention. A human decides, and the
        cost is that a document which failed permanently and has since been *fixed at the
        source* needs an explicit requeue. That is the safer direction, and it is stated
        here rather than discovered.

    `attempts` is reset because this is genuinely new work on new content, not another try
    at the attempt that already succeeded.
    """
    row = (
        await session.execute(
            text(
                "UPDATE jobs SET state = :pending, attempts = 0, locked_until = NULL, "
                "next_attempt_at = NULL, error = NULL, failure_kind = NULL, updated_at = now() "
                "WHERE idempotency_key = :key AND state = :completed "
                "RETURNING id"
            ),
            {
                "pending": JobState.PENDING.value,
                "completed": JobState.COMPLETED.value,
                "key": idempotency_key,
            },
        )
    ).first()
    return row is not None


def _job_from_row(row: Any) -> Job:
    payload = (
        row.payload_json if isinstance(row.payload_json, dict) else json.loads(row.payload_json)
    )
    return Job(
        id=uuid.UUID(str(row.id)),
        org_id=uuid.UUID(str(row.org_id)),
        kind=JobKind(row.kind),
        state=JobState(row.state),
        idempotency_key=row.idempotency_key,
        payload=payload,
        attempts=row.attempts,
    )


async def claim_job(
    session: AsyncSession,
    *,
    kind: JobKind,
    job_id: uuid.UUID | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Job | None:
    """Take one runnable job of `kind`, or return None.

    `FOR UPDATE SKIP LOCKED` is what lets many workers share a queue without coordinating:
    a row another transaction is holding is skipped rather than waited on, so throughput
    does not collapse to serial when one document is slow.

    A claim does three things atomically — increments `attempts`, sets the lease, and moves
    the row out of the claimable states — so a job cannot be claimed twice even if two
    workers ask in the same instant.

    `job_id` narrows the claim to one row, which is the dispatch path: arq says "job X in
    org Y", the worker scopes to Y and claims X. If Y is wrong the row is invisible under
    RLS and this returns None. **A forged dispatch message can therefore waste a worker's
    time and nothing else.**

    Without `job_id` it takes the oldest runnable job, which is the recovery path.
    """
    row = (
        await session.execute(
            text(
                "UPDATE jobs SET "
                "  state = :running, "
                "  attempts = attempts + 1, "
                # `make_interval` rather than an interpolated INTERVAL literal: the lease
                # is then a bound parameter like every other value here, so there is no
                # string-built SQL in the claim path at all.
                "  locked_until = now() + make_interval(secs => :lease), "
                "  updated_at = now() "
                "WHERE id = ("
                "  SELECT j.id FROM jobs j "
                "  WHERE j.kind = :kind "
                "    AND j.state = ANY(:claimable) "
                "    AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= now()) "
                "    AND (j.locked_until IS NULL OR j.locked_until <= now()) "
                "    AND (CAST(:job_id AS uuid) IS NULL OR j.id = CAST(:job_id AS uuid)) "
                "  ORDER BY j.created_at, j.id "
                "  FOR UPDATE SKIP LOCKED LIMIT 1"
                ") "
                "RETURNING id, org_id, kind, state, idempotency_key, payload_json, attempts"
            ),
            {
                # The row moves straight into its first working state. Leaving it
                # `pending` while a worker held it would make a crashed claim
                # indistinguishable from work nobody had started.
                "running": JobState.FETCHING.value,
                "kind": kind.value,
                "claimable": [state.value for state in CLAIMABLE],
                "job_id": str(job_id) if job_id is not None else None,
                "lease": lease_seconds,
            },
        )
    ).first()
    return _job_from_row(row) if row is not None else None


async def record_state(
    session: AsyncSession, *, job_id: uuid.UUID, state: JobState, extend_lease_by: int | None = None
) -> None:
    """Move a job to its next progress state and keep its lease alive.

    Called as each stage completes, so a job that stops says which stage it reached rather
    than only that it stopped. Extending the lease here is what stops a long but healthy
    document being reclaimed out from under the worker still working on it.
    """
    await session.execute(
        text(
            "UPDATE jobs SET state = :state, updated_at = now(), "
            # A CASE rather than two statements or a built fragment, so the whole
            # update stays one bound-parameter query whichever branch applies.
            "locked_until = CASE WHEN CAST(:lease AS integer) IS NULL THEN locked_until "
            "ELSE now() + make_interval(secs => CAST(:lease AS integer)) END "
            "WHERE id = :id"
        ),
        {"state": state.value, "lease": extend_lease_by, "id": str(job_id)},
    )


async def complete_job(session: AsyncSession, *, job_id: uuid.UUID) -> None:
    """Terminal success. Clears the lease and the failure fields.

    The failure fields are cleared rather than left, because a job that succeeded on its
    third attempt should not read as one that failed twice and is still failing.
    """
    await session.execute(
        text(
            "UPDATE jobs SET state = :state, locked_until = NULL, next_attempt_at = NULL, "
            "error = NULL, failure_kind = NULL, updated_at = now() WHERE id = :id"
        ),
        {"state": JobState.COMPLETED.value, "id": str(job_id)},
    )


async def fail_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    failure_kind: FailureKind,
    message: str,
    attempts: int,
    retryable: bool,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    rng: random.Random | None = None,
) -> JobState:
    """Record a failure and decide what happens next. Returns the state it landed in.

    Three outcomes, and the classification is the caller's because only the caller knows
    what threw:

      * **not retryable** -> `FAILED`. A malformed document and a 400 from the provider
        are both rejected identically every time; retrying spends real resources to be
        told so again.
      * **retryable, attempts left** -> `RETRY_SCHEDULED` with `next_attempt_at` set by
        jittered exponential backoff.
      * **retryable, attempts exhausted** -> `DEAD_LETTER`, which is a state a human is
        expected to look at rather than a loop that never ends.

    `message` is an error *description* and must never carry document content, a chunk, a
    principal or a provider response body — it is stored and it is logged. Callers pass a
    classification and a shape, not a payload.
    """
    if not retryable:
        state = JobState.FAILED
        next_attempt = None
    elif attempts >= max_attempts:
        state = JobState.DEAD_LETTER
        next_attempt = None
    else:
        state = JobState.RETRY_SCHEDULED
        next_attempt = datetime.now(UTC) + timedelta(seconds=backoff_delay(attempts, rng=rng))

    await session.execute(
        text(
            "UPDATE jobs SET state = :state, failure_kind = :kind, error = :error, "
            "next_attempt_at = :next_attempt, locked_until = NULL, updated_at = now() "
            "WHERE id = :id"
        ),
        {
            "state": state.value,
            "kind": failure_kind.value,
            "error": message[:2000],
            "next_attempt": next_attempt,
            "id": str(job_id),
        },
    )
    return state


async def reclaim_expired_leases(
    session: AsyncSession, *, kinds: Sequence[JobKind] | None = None
) -> int:
    """Return crashed jobs to the queue. Runs inside one organisation's scope.

    A worker that is killed mid-job leaves a row in a working state with a lease that
    stops being renewed. Nothing needs to detect the crash: the lease expiring *is* the
    detection, which is why there is no heartbeat to miss and no cleanup handler that must
    survive a `SIGKILL` to run.

    Reclaimed rows go to `RETRY_SCHEDULED` with `next_attempt_at` cleared, so they are
    runnable immediately — the attempt was already counted when the job was claimed, so a
    crash-looping job still walks the same bounded ladder to `DEAD_LETTER` rather than
    retrying for ever.

    **Org-scoped, and that is a real limitation rather than an oversight.** See the module
    docstring: nothing can read `jobs` across tenants under FORCE RLS, so this recovers the
    tenant whose scope is set and no other.
    """
    selected = [kind.value for kind in (kinds or tuple(JobKind))]
    working = [
        state.value for state in JobState if state not in TERMINAL and state not in CLAIMABLE
    ]

    rows = (
        await session.execute(
            text(
                "UPDATE jobs SET state = :retry, next_attempt_at = NULL, locked_until = NULL, "
                "failure_kind = :kind, error = :error, updated_at = now() "
                "WHERE kind = ANY(:kinds) AND state = ANY(:working) "
                "  AND locked_until IS NOT NULL AND locked_until <= now() "
                "RETURNING id"
            ),
            {
                "retry": JobState.RETRY_SCHEDULED.value,
                "kinds": selected,
                "working": working,
                "kind": FailureKind.INTERNAL.value,
                "error": "lease expired; reclaimed for retry",
            },
        )
    ).all()
    return len(rows)
