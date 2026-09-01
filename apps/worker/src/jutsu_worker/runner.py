"""Transaction boundaries for one unit of work. The part that is easy to get wrong.

Each job passes through **three separate transactions**, and the separation is the whole
point of this module rather than an implementation detail:

    1. claim    — commits immediately, so the attempt count and the lease are durable
    2. work     — the fetch, the writes, the audit row
    3. failure  — only if 2 raised, in a *fresh* transaction

**The claim commits before the work starts, and that is what makes bounded attempts
bounded.** Claiming and working in one transaction looks tidier and is wrong: a failure
rolls the whole thing back, including the `attempts + 1`, so the job returns to the queue
looking untouched and retries for ever. The ladder to `dead_letter` only counts if the
counting survives the failure it is counting.

**The failure is recorded in a new transaction because the old one is dead.** Postgres
aborts a transaction at the first error, and every statement after that raises
`InFailedSqlTransaction` — so a handler that tries to write `failure_kind` into the same
transaction that just failed silently records nothing and re-raises something unrelated.
Rolling back first is not tidiness; it is the difference between a classified failure and
a job stuck in `fetching` until its lease expires.

**Embedding writes its vectors in a transaction of its own too**, opened by
`embed_pending_chunks`. A crash between that commit and the job's completion leaves
vectors stored and the job claimable again — and the re-run embeds nothing, because
pending work is `WHERE embedding IS NULL`. Resumability is the selection, so the two
transactions do not need to be one.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final, final

from jutsu_db.engine import org_session
from jutsu_retrieval.embeddings import Embedder

from jutsu_worker.extraction import (
    AnthropicExtractionTransport,
    ExtractionTransport,
    extract_document,
)
from jutsu_worker.ingest import (
    record_failure,
    run_document_job,
    run_embedding_job,
    run_source_job,
)
from jutsu_worker.jobs import Job, JobKind, JobState, claim_job
from jutsu_worker.pipeline import IngestOutcome
from jutsu_worker.sync import mark_sync_unavailable, run_sync_job

__all__ = [
    "JOB_FAILED",
    "process_connector_sync",
    "process_document",
    "process_embedding",
    "process_extraction",
    "process_source",
]

logger = logging.getLogger("jutsu.worker.runner")


async def _claim(org_id: uuid.UUID, kind: JobKind, job_id: uuid.UUID | None) -> Job | None:
    """Transaction 1. Commits on exit, so the lease and the attempt survive a crash."""
    async with org_session(org_id) as session:
        return await claim_job(session, kind=kind, job_id=job_id)


async def _record_failure(job: Job, error: BaseException) -> JobState:
    """Transaction 3. A new one, because the work transaction is already aborted."""
    async with org_session(job.org_id) as session:
        return await record_failure(session, job=job, error=error)


async def process_source(
    org_id: uuid.UUID, source_id: uuid.UUID, *, correlation_id: str | None = None
) -> bool:
    """Walk one source. Returns whether a job was claimed and run.

    The walk, the enqueued document jobs and the cursor advance share one transaction, so
    a crash anywhere in the walk leaves the cursor where it was and the work is simply
    listed again next time.
    """
    job = await _claim(org_id, JobKind.INGEST_SOURCE, None)
    if job is None:
        return False

    try:
        async with org_session(org_id) as session:
            await run_source_job(
                session,
                org_id=org_id,
                source_id=uuid.UUID(str(job.payload["source_id"])),
                correlation_id=correlation_id or job.correlation_id,
            )
            from jutsu_worker.jobs import complete_job

            await complete_job(session, job_id=job.id)
    except Exception as error:
        await _record_failure(job, error)
    return True


@final
class _JobFailed:
    """A job was claimed and its attempt failed.

    Its own type because the previous contract returned `None` for this *and* for "the
    queue had nothing to claim", and a drain loop cannot act correctly on an answer that
    conflates them. The old docstring argued the caller's next action was the same. It is
    not: on an empty queue the caller stops, on a failed job it moves to the next one.

    Measured cost of the conflation: the 200-document pilot embedded 12 documents, met one
    HTTP 429, took the `None` as "queue empty", stopped, and exited **0** with 187 jobs
    never attempted. A 45 000-document run would report success the same way.

    Falsy, so `if outcome:` at an existing call site still treats a failure as "nothing
    useful happened" rather than silently becoming truthy.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "JOB_FAILED"


#: Singleton. Compare with `is`, never with `==`.
JOB_FAILED: Final = _JobFailed()


async def process_document(
    org_id: uuid.UUID, *, job_id: uuid.UUID | None = None
) -> IngestOutcome | _JobFailed | None:
    """Fetch, mask, chunk and store one document.

    Returns the outcome on success, `JOB_FAILED` if a claimed job failed (the failure is
    recorded on the row before returning), and `None` only when nothing was claimable.
    """
    job = await _claim(org_id, JobKind.INGEST_DOCUMENT, job_id)
    if job is None:
        return None

    try:
        async with org_session(org_id) as session:
            return await run_document_job(session, job=job)
    except Exception as error:
        state = await _record_failure(job, error)
        logger.info("document_job_failed job=%s state=%s", job.id, state.value)
        return JOB_FAILED


async def process_embedding(
    org_id: uuid.UUID, embedder: Embedder, *, job_id: uuid.UUID | None = None
) -> int | _JobFailed | None:
    """Embed one document's pending chunks.

    Returns vectors written on success, `JOB_FAILED` if a claimed job failed, and `None`
    only when nothing was claimable.

    A failure here can never cause a re-fetch: the document job committed before this job
    existed, and nothing in this path touches an `ingest.document` row.
    """
    job = await _claim(org_id, JobKind.EMBED_DOCUMENT, job_id)
    if job is None:
        return None

    try:
        async with org_session(org_id) as session:
            return await run_embedding_job(session, job=job, embedder=embedder)
    except Exception as error:
        state = await _record_failure(job, error)
        logger.info("embedding_job_failed job=%s state=%s", job.id, state.value)
        return JOB_FAILED


async def process_connector_sync(
    org_id: uuid.UUID, *, job_id: uuid.UUID | None = None
) -> int | _JobFailed | None:
    """Run one queued sync. Returns documents fetched, JOB_FAILED, or None if idle.

    The connection annotation happens in a FOURTH transaction, after the failure is
    recorded: the work transaction is dead, and the failure transaction belongs to the
    job row — mixing the connection update into it would couple the queue's bookkeeping
    to a data-plane row it does not own.
    """
    job = await _claim(org_id, JobKind.CONNECTOR_SYNC, job_id)
    if job is None:
        return None

    try:
        async with org_session(org_id) as session:
            fetched = await run_sync_job(session, job=job)
            from jutsu_worker.jobs import complete_job

            await complete_job(session, job_id=job.id)
            return fetched
    except Exception as error:
        state = await _record_failure(job, error)
        connection_id = job.payload.get("connection_id")
        if connection_id:
            async with org_session(org_id) as session:
                await mark_sync_unavailable(session, connection_id=uuid.UUID(str(connection_id)))
        logger.info("sync_job_failed job=%s state=%s", job.id, state.value)
        return JOB_FAILED


async def process_extraction(
    org_id: uuid.UUID,
    *,
    job_id: uuid.UUID | None = None,
    transport: ExtractionTransport | None = None,
) -> int | _JobFailed | None:
    """Run one queued extraction. Returns claims stored, JOB_FAILED, or None if idle.

    The transport is injectable for the same reason every paid provider here is: the
    quote gate has to be tested against a model that misbehaves on purpose.
    """
    job = await _claim(org_id, JobKind.EXTRACT_DOCUMENT, job_id)
    if job is None:
        return None

    try:
        async with org_session(org_id) as session:
            result = await extract_document(
                session,
                org_id=org_id,
                document_id=uuid.UUID(str(job.payload["document_id"])),
                transport=transport or AnthropicExtractionTransport(),
            )
            from jutsu_worker.jobs import complete_job

            await complete_job(session, job_id=job.id)
            return result.stored
    except Exception as error:
        state = await _record_failure(job, error)
        logger.info("extraction_job_failed job=%s state=%s", job.id, state.value)
        return JOB_FAILED
