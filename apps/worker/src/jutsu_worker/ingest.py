"""The three durable stages, and the audit trail they leave (§9, §17).

    ingest.source  ->  ingest.document  ->  embed.document

**They are three job rows, not three phases of one.** §9 requires that a failure at one
stage never forces another to re-run, and the boundary that matters is embedding: it is
the stage that spends money and the stage most likely to fail transiently. A provider 429
must never cause the source to be read again, and the only way to guarantee that
structurally is for the fetch to be finished and committed before the embedding job
exists.

**The source cursor advances in the same transaction that enqueues the work.** Advance it
first and a crash loses documents silently; advance it after a separate commit and a crash
between the two does the same. One transaction means the cursor and the jobs are the same
fact, so the failure mode is re-enqueueing work that is already idempotent rather than
skipping work nobody will look at again.

**The cursor is taken before the walk, not after.** A file modified while the walk is in
progress would otherwise fall between the two instants and never be seen. Taking the
earlier value re-lists it next run, which costs one `unchanged` outcome and no writes.

**Recovery is org-scoped and runs here.** Each source run reclaims its own organisation's
expired leases before enqueueing anything. There is no global sweeper, and that is a
consequence of the tenant isolation rather than an oversight: under `FORCE ROW LEVEL
SECURITY` nothing can read `jobs` across tenants, not even a `SECURITY DEFINER` function
(ADR 0012). **An organisation whose source is never processed again keeps its orphaned
jobs**, and closing that is the job of the multi-tenant scheduler in a later slice.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anthropic
from jutsu_connectors import PathEscape, UnparsableMessage
from jutsu_core import SourceSystem
from jutsu_retrieval.embeddings import Embedder
from jutsu_retrieval.errors import (
    EmbeddingBudgetExceeded,
    PermanentEmbeddingError,
    TransientEmbeddingError,
    TruncatedInput,
)
from jutsu_retrieval.persistence import embed_pending_chunks
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_worker.credentials import (
    CredentialsUnavailable,
    ReauthRequired,
    TransientRefreshError,
)
from jutsu_worker.extraction import extraction_configured, extraction_job_key
from jutsu_worker.jobs import (
    FailureKind,
    Job,
    JobKind,
    JobState,
    claim_job,
    complete_job,
    enqueue_job,
    fail_job,
    reclaim_expired_leases,
    record_state,
    reopen_completed_job,
)
from jutsu_worker.pipeline import IngestOutcome, persist_document
from jutsu_worker.registry import UnsupportedSource, connector_for

__all__ = [
    "SourceRun",
    "document_job_key",
    "embed_job_key",
    "run_document_job",
    "run_embedding_job",
    "run_source_job",
    "source_job_key",
]

#: Counts, states and opaque ids. Never a body, a chunk, a vector, a principal or a
#: provider response — §4.9, and every one of those passes through this module.
logger = logging.getLogger("jutsu.worker.ingest")


@dataclass(frozen=True, slots=True)
class SourceRun:
    """What one source walk did. Numbers only, safe to log and to assert on."""

    listed: int
    enqueued: int
    #: Jobs that already existed and were left alone — queued, in flight, or failed.
    duplicate: int
    #: Completed jobs made runnable again, because a walk asks "has this changed?".
    reopened: int
    reclaimed: int
    cursor: str | None


# --------------------------------------------------------------------------------------
# Idempotency keys. Deterministic functions of identity — never a timestamp, never a UUID4.
# --------------------------------------------------------------------------------------


def source_job_key(org_id: uuid.UUID, source_id: uuid.UUID) -> str:
    """One walk per source at a time.

    Two concurrent walks of the same source would enqueue the same documents twice and
    race on the cursor. The unique constraint makes the second one a no-op instead.
    """
    return f"{JobKind.INGEST_SOURCE.value}:{org_id}:{source_id}"


def document_job_key(org_id: uuid.UUID, source_id: uuid.UUID, external_id: str) -> str:
    """One job per document identity — **not** per version.

    The content hash is deliberately absent. The unit of work is "make this document
    current", so a document whose body changes must reuse the same job rather than
    accumulate one row per revision. Content identity is enforced where it belongs, inside
    `persist_document`, which compares `content_hash` and writes nothing when it matches.

    Including a hash here would also be impossible without fetching first, which is the
    stage this key exists to schedule.
    """
    return f"{JobKind.INGEST_DOCUMENT.value}:{org_id}:{source_id}:{external_id}"


def embed_job_key(org_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """One embedding job per document **version**.

    The document id already is the version — a new version is a new row with a new id — so
    this is content-addressed without needing the hash, and re-running an unchanged
    document enqueues nothing new.
    """
    return f"{JobKind.EMBED_DOCUMENT.value}:{org_id}:{document_id}"


# --------------------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------------------


async def _audit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: str,
    correlation_id: str,
    outcome: str,
    meta: dict[str, Any] | None = None,
) -> None:
    """One immutable row per pipeline event (§17).

    `actor_type` is `system`: no human initiated this, and attributing a background run to
    a user would make the trail lie about who did what. `actor_id` is therefore NULL.

    `meta` carries counts and states. **It must never carry a title, a body, an address or
    a principal** — the audit log is exported, and §4.9 governs it exactly as it governs
    the log lines.
    """
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome, correlation_id, meta_json) "
            "VALUES (:org, NULL, 'system', :action, :rtype, :rid, :outcome, :corr, "
            "CAST(:meta AS jsonb))"
        ),
        {
            "org": str(org_id),
            "action": action,
            "rtype": resource_type,
            "rid": resource_id,
            "outcome": outcome,
            "corr": correlation_id,
            "meta": json.dumps(meta or {}),
        },
    )


# --------------------------------------------------------------------------------------
# ingest.source
# --------------------------------------------------------------------------------------


async def _load_source(session: AsyncSession, source_id: uuid.UUID) -> Any:
    """The source row, inside the caller's tenant scope.

    No `org_id` predicate: the policy supplies it. A source id belonging to another tenant
    simply is not found, which is the correct answer and the same one an unknown id gets.
    """
    return (
        await session.execute(
            text("SELECT id, system, config_json, last_sync_cursor FROM sources WHERE id = :id"),
            {"id": str(source_id)},
        )
    ).first()


async def run_source_job(
    session: AsyncSession, *, org_id: uuid.UUID, source_id: uuid.UUID, correlation_id: str
) -> SourceRun:
    """Walk one source, enqueue a document job per identifier, advance the cursor.

    Runs entirely inside the caller's transaction. The caller commits, and that single
    commit is what makes the cursor and the enqueued jobs atomic with each other — the
    property that stops a crash losing documents.

    Reclaims this organisation's expired leases first, which is where crash recovery
    happens in the absence of a global sweeper.
    """
    started = datetime.now(UTC)
    reclaimed = await reclaim_expired_leases(session)

    row = await _load_source(session, source_id)
    if row is None:
        raise UnsupportedSource("source not found in this organisation")

    connector = connector_for(SourceSystem(row.system), row.config_json or {})

    listed = 0
    enqueued = 0
    duplicate = 0
    reopened = 0
    async for external_id in connector.list_since(row.last_sync_cursor):
        listed += 1
        key = document_job_key(org_id, source_id, external_id)
        created = await enqueue_job(
            session,
            org_id=org_id,
            kind=JobKind.INGEST_DOCUMENT,
            idempotency_key=key,
            payload={"source_id": str(source_id), "external_id": external_id},
        )
        if created is None:
            # The key already exists, which means one of two very different things.
            #
            # If the previous run *finished* it, this walk is asking the same question
            # again — has this document changed? — so the job is reopened. Without that a
            # completed job would shadow the identifier for ever and no later edit would
            # ever be ingested.
            #
            # If it is queued, in flight or permanently failed, it is left exactly as it
            # is: re-creating it would duplicate work or restart a loop that already gave
            # up for a reason.
            if await reopen_completed_job(session, idempotency_key=key):
                reopened += 1
            else:
                duplicate += 1
        else:
            enqueued += 1

    cursor = started.isoformat()
    await session.execute(
        text("UPDATE sources SET last_sync_cursor = :cursor, last_sync_at = now() WHERE id = :id"),
        {"cursor": cursor, "id": str(source_id)},
    )

    await _audit(
        session,
        org_id=org_id,
        action="ingest.source.completed",
        resource_type="source",
        resource_id=str(source_id),
        correlation_id=correlation_id,
        outcome="success",
        meta={
            "listed": listed,
            "enqueued": enqueued,
            "duplicate": duplicate,
            "reopened": reopened,
            "reclaimed": reclaimed,
        },
    )
    # The walk's numbers land on the source row too (§11): the Knowledge Sources UI
    # reads measured stage counters from here rather than reconstructing them from
    # job rows. Same transaction as the cursor advance, so the stats and the cursor
    # can never describe different walks.
    await session.execute(
        text("UPDATE sources SET stats_json = stats_json || cast(:stats AS jsonb) WHERE id = :id"),
        {
            "stats": json.dumps(
                {
                    "last_walk": {
                        "listed": listed,
                        "enqueued": enqueued,
                        "duplicate": duplicate,
                        "reopened": reopened,
                        "reclaimed": reclaimed,
                    }
                }
            ),
            "id": str(source_id),
        },
    )
    logger.info(
        "source_run org=%s source=%s listed=%d enqueued=%d reopened=%d duplicate=%d reclaimed=%d",
        org_id,
        source_id,
        listed,
        enqueued,
        reopened,
        duplicate,
        reclaimed,
    )
    return SourceRun(
        listed=listed,
        enqueued=enqueued,
        duplicate=duplicate,
        reopened=reopened,
        reclaimed=reclaimed,
        cursor=cursor,
    )


# --------------------------------------------------------------------------------------
# ingest.document
# --------------------------------------------------------------------------------------


async def run_document_job(session: AsyncSession, *, job: Job) -> IngestOutcome:
    """Fetch one document, mask it, chunk it, store it, and queue its embedding.

    The state is recorded as each stage completes, so a job that stops says *where*. The
    sequence is §9's — `chunked` is where the document, its grants and its chunks are
    written, because they must exist before anything can embed them, and `persisted` is
    reached once the embedding job is queued.

    **The embedding job is enqueued here, in this transaction.** That is what makes the
    two stages independent: once this commits, the fetched document is durable and the
    embedding work exists as its own row. Nothing that happens to the embedding can reach
    back and cause another fetch.

    An unchanged document enqueues nothing. Its chunks are either already embedded or
    already queued, and creating a second embedding job for the same version would be work
    with no output.
    """
    org_id = job.org_id
    source_id = uuid.UUID(str(job.payload["source_id"]))
    external_id = str(job.payload["external_id"])

    row = await _load_source(session, source_id)
    if row is None:
        raise UnsupportedSource("source not found in this organisation")
    connector = connector_for(SourceSystem(row.system), row.config_json or {})

    raw = await connector.fetch(external_id)
    await record_state(session, job_id=job.id, state=JobState.NORMALIZED)

    # The grants come from the connector, which derives them from the document's own
    # participants (ADR 0008). Nothing here adds, defaults or widens them.
    await record_state(session, job_id=job.id, state=JobState.ACL_CAPTURED)
    await record_state(session, job_id=job.id, state=JobState.MASKED)

    persisted = await persist_document(session, org_id=org_id, source_id=source_id, raw=raw)
    await record_state(session, job_id=job.id, state=JobState.CHUNKED)

    if persisted.outcome is not IngestOutcome.UNCHANGED:
        await enqueue_job(
            session,
            org_id=org_id,
            kind=JobKind.EMBED_DOCUMENT,
            idempotency_key=embed_job_key(org_id, persisted.document_id),
            payload={"document_id": str(persisted.document_id)},
        )

    await record_state(session, job_id=job.id, state=JobState.PERSISTED)
    await _audit(
        session,
        org_id=org_id,
        action=f"ingest.document.{persisted.outcome.value}",
        resource_type="document",
        resource_id=str(persisted.document_id),
        correlation_id=job.correlation_id,
        outcome="success",
        meta={
            "chunks": persisted.chunk_count,
            "grants": persisted.acl_count,
            "superseded": str(persisted.superseded_id) if persisted.superseded_id else None,
        },
    )
    await complete_job(session, job_id=job.id)
    return persisted.outcome


# --------------------------------------------------------------------------------------
# embed.document
# --------------------------------------------------------------------------------------


async def run_embedding_job(
    session: AsyncSession, *, job: Job, embedder: Embedder, limit: int = 1000
) -> int:
    """Embed this document's pending chunks. Returns how many vectors were written.

    Delegates to S6's `embed_pending_chunks`, which selects `WHERE embedding IS NULL` —
    so a partial failure resumes by asking the same question again, and a completed
    document costs one indexed query and no provider call. Every S6 protection is
    inherited rather than reimplemented: 768 dimensions, L2 normalisation, the query/
    document task-type split, the provider's own token accounting, truncation rejection,
    batch bounds, jittered retry and the token budget.

    Scoped to one document so a single poison chunk cannot stall the whole corpus.
    """
    document_id = uuid.UUID(str(job.payload["document_id"]))
    await record_state(session, job_id=job.id, state=JobState.EMBEDDING)

    run = await embed_pending_chunks(job.org_id, embedder, limit=limit, document_id=document_id)

    await record_state(session, job_id=job.id, state=JobState.PERSISTED)

    # Embedding done means the document is retrievable; extraction is the next stage,
    # and it is queued here — in the same transaction as this job's completion — so a
    # crash between the two re-runs the idempotent enqueue rather than losing it.
    # Gated on configuration at ENQUEUE time: on a deployment with no extraction
    # provider, queueing jobs whose only possible outcome is failure would fill the
    # dead-letter view with noise about a fact the operator already knows.
    if extraction_configured():
        await enqueue_job(
            session,
            org_id=job.org_id,
            kind=JobKind.EXTRACT_DOCUMENT,
            idempotency_key=extraction_job_key(job.org_id, document_id),
            payload={"document_id": str(document_id)},
        )

    await _audit(
        session,
        org_id=job.org_id,
        action="embed.document.completed",
        resource_type="document",
        resource_id=str(document_id),
        correlation_id=job.correlation_id,
        outcome="success",
        meta={"embedded": run.embedded, "tokens": run.tokens, "requests": run.requests},
    )
    await complete_job(session, job_id=job.id)
    logger.info(
        "embed_document org=%s document=%s embedded=%d tokens=%d requests=%d",
        job.org_id,
        document_id,
        run.embedded,
        run.tokens,
        run.requests,
    )
    return run.embedded


# --------------------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------------------


def classify(error: BaseException) -> tuple[FailureKind, bool]:
    """Map an exception to `(failure_kind, retryable)`.

    The classification is the difference between a queue that drains and one that spins.
    Each answer is a different operator action, which is why they are distinct states:

      * a **malformed** document will never parse, so retrying burns attempts to reach the
        same conclusion five times;
      * a **path escape** is a security refusal, not a transient read error, and must not
        be retried into succeeding;
      * a **permanent** embedding error is rejected identically every time, and on a
        corpus-sized job retrying it spends real quota to be told so again;
      * a **budget** exhaustion is not a fault in the document at all — the run hit its
        ceiling — so it is retryable once the ceiling moves.

    Anything unrecognised is `INTERNAL` and retryable, which is the safe direction: an
    unknown failure that is actually permanent costs a bounded number of attempts and then
    dead-letters, while an unknown failure treated as permanent would silently drop work.
    """
    if isinstance(error, UnparsableMessage):
        return FailureKind.MALFORMED_DOCUMENT, False
    if isinstance(error, PathEscape | UnsupportedSource):
        return FailureKind.SOURCE_UNAVAILABLE, False
    if isinstance(error, TruncatedInput | PermanentEmbeddingError):
        return FailureKind.EMBEDDING_PERMANENT, False
    if isinstance(error, TransientEmbeddingError):
        return FailureKind.EMBEDDING_TRANSIENT, True
    if isinstance(error, EmbeddingBudgetExceeded):
        return FailureKind.BUDGET_EXHAUSTED, True
    if isinstance(error, ReauthRequired | CredentialsUnavailable):
        return FailureKind.PROVIDER_PERMANENT, False
    if isinstance(error, TransientRefreshError):
        return FailureKind.PROVIDER_TRANSIENT, True
    # Anthropic SDK errors from the extraction transport. Order matters: RateLimitError
    # and InternalServerError are both APIStatusError subclasses, so the transient checks
    # come first and the remaining 4xx statuses are permanent — a bad key or a nonexistent
    # model is rejected identically every time, and retrying it five times burns real
    # attempts to be told so five times.
    if isinstance(
        error,
        anthropic.RateLimitError | anthropic.InternalServerError | anthropic.APIConnectionError,
    ):
        return FailureKind.PROVIDER_TRANSIENT, True
    if isinstance(error, anthropic.APIStatusError):
        return FailureKind.PROVIDER_PERMANENT, False
    if isinstance(error, FileNotFoundError | OSError):
        return FailureKind.SOURCE_UNAVAILABLE, True
    return FailureKind.INTERNAL, True


async def record_failure(session: AsyncSession, *, job: Job, error: BaseException) -> JobState:
    """Classify, persist and audit a failed attempt.

    The stored message is `type: str(error)` — a description, never a payload. Connector
    and provider exceptions are written by code that does not carry document text in them,
    and this is the boundary that has to keep it that way, because `jobs.error` is read by
    operators and exported with the audit trail.
    """
    kind, retryable = classify(error)
    message = f"{type(error).__name__}: {error}"

    state = await fail_job(
        session,
        job_id=job.id,
        failure_kind=kind,
        message=message,
        attempts=job.attempts,
        retryable=retryable,
    )
    await _audit(
        session,
        org_id=job.org_id,
        action=f"{job.kind.value}.failed",
        resource_type="job",
        resource_id=str(job.id),
        correlation_id=job.correlation_id,
        # `outcome` is the audit log's own three-value vocabulary, constrained by
        # migration 0002 to success / denied / failure. It is not the job state, and
        # writing one into the other is what the CHECK constraint caught: the state is a
        # pipeline detail and belongs in `meta`, where an operator can still read it.
        outcome="failure",
        meta={
            "state": state.value,
            "failure_kind": kind.value,
            "attempts": job.attempts,
            "retryable": retryable,
        },
    )
    logger.warning(
        "job_failed org=%s job=%s kind=%s failure=%s state=%s attempts=%d",
        job.org_id,
        job.id,
        job.kind.value,
        kind.value,
        state.value,
        job.attempts,
    )
    return state


async def claim_and_run_document(
    session: AsyncSession, *, job_id: uuid.UUID | None = None
) -> IngestOutcome | None:
    """Claim one document job and run it, recording whatever happens. For the worker loop.

    Returns None when there was nothing to claim, which is the ordinary idle case rather
    than an error.
    """
    job = await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=job_id)
    if job is None:
        return None
    try:
        return await run_document_job(session, job=job)
    except Exception as error:
        await record_failure(session, job=job, error=error)
        return None
