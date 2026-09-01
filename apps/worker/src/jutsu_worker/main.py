"""Worker entry point.

The ingestion pipeline (§9) runs here as separately queued stages, so a failure at
`extract` never forces a re-`fetch`. S0 only proves the queue interface resolves; the
stages land in S8.

**Maintenance runs here too, and the first job is not optional.** Migration 0007 gives
`auth.pending_registrations` an `expires_at`, and an expiry column with nothing deleting
it is a comment rather than a control: the rows stop being *usable* on their own — the
consuming function filters on `expires_at > now()` — but they do not stop *existing*.
Each one holds a name, a work address and a job title in a schema that sits outside every
tenant boundary, so leaving them is a data-retention problem, not a correctness one.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from jutsu_db import unscoped_session
from jutsu_retrieval.client import VertexTransport
from jutsu_retrieval.config import get_embedding_settings
from jutsu_retrieval.embeddings import Embedder
from sqlalchemy import text

from jutsu_worker.pipeline import IngestOutcome
from jutsu_worker.runner import (
    process_connector_sync,
    process_document,
    process_embedding,
    process_extraction,
    process_source,
)

DEFAULT_REDIS_URL = "redis://localhost:6379"

logger = logging.getLogger("jutsu.worker")

#: Every five minutes. The challenge TTL is ten, so an abandoned registration's details
#: exist for at most about a quarter of an hour rather than indefinitely. The work is one
#: indexed DELETE against a table that is empty most of the time, so a tighter schedule
#: costs effectively nothing and shortens how long personal data sits in `auth`.
#: A plain `set`, not a `frozenset`: arq's `cron` is typed `set[int] | int | None` and
#: an immutable one is not a subtype of it.
REAP_MINUTES = set(range(0, 60, 5))


async def ping(ctx: dict[str, Any]) -> str:
    """No-op job. Exists so the queue wiring is exercised before S8 depends on it."""
    return "pong"


async def ingest_source(ctx: dict[str, Any], org_id: str, source_id: str) -> bool:
    """Dispatch entry point for a source walk.

    **The arguments are a hint about where to look, never an authorization.** `org_id`
    scopes the database session; row-level security then decides what that scope can see.
    A forged message naming another tenant reaches no row of theirs — it finds nothing in
    its own and returns, which is a wasted round trip rather than a leak.
    """
    return await process_source(uuid.UUID(org_id), uuid.UUID(source_id))


async def ingest_document(
    ctx: dict[str, Any], org_id: str, job_id: str | None = None
) -> str | None:
    """Dispatch entry point for one document.

    Redis may deliver this twice, late, or never. Twice is refused by the lease; late is
    harmless; never is recovered by the next source run, because the durable job row is
    what the work actually is.
    """
    outcome = await process_document(
        uuid.UUID(org_id), job_id=uuid.UUID(job_id) if job_id else None
    )
    # `JOB_FAILED` and `None` both mean "no outcome to report" to the dispatcher; the
    # job row carries what actually happened.
    return outcome.value if isinstance(outcome, IngestOutcome) else None


async def embed_document(ctx: dict[str, Any], org_id: str, job_id: str | None = None) -> int | None:
    """Dispatch entry point for one document's embeddings.

    Builds the provider transport per job rather than holding one open: a worker that is
    idle overnight should not be holding an authenticated connection pool, and the
    settings are read fresh so a rotated credential takes effect without a redeploy.
    """
    settings = get_embedding_settings()
    transport = VertexTransport(settings)
    try:
        result = await process_embedding(
            uuid.UUID(org_id),
            Embedder(transport, settings),
            job_id=uuid.UUID(job_id) if job_id else None,
        )
        return result if isinstance(result, int) else None
    finally:
        await transport.aclose()


async def sync_connection(ctx: dict[str, Any], org_id: str, job_id: str | None = None) -> None:
    """Dispatch entry point for one queued connector sync.

    Same authorization stance as every dispatch function here: the arguments hint at
    where to look, row-level security decides what that scope can see.
    """
    await process_connector_sync(uuid.UUID(org_id), job_id=uuid.UUID(job_id) if job_id else None)


async def extract_document_job(
    ctx: dict[str, Any], org_id: str, job_id: str | None = None
) -> int | None:
    """Dispatch entry point for one queued extraction."""
    result = await process_extraction(
        uuid.UUID(org_id), job_id=uuid.UUID(job_id) if job_id else None
    )
    return result if isinstance(result, int) else None


async def reap_expired_registrations(ctx: dict[str, Any]) -> int:
    """Delete staged registrations, challenges and rate-limit rows that have aged out.

    `unscoped_session` on purpose, and it is the sanctioned use rather than a shortcut:
    these tables carry no `org_id` at all, so there is no tenant to scope to. Its
    docstring names maintenance of exactly this kind as legitimate.

    Returns the number of pending registrations removed so a run is observable. The count
    is a number of rows and never their contents — §4.9 keeps names and addresses out of
    logs, and this is the one job that handles both.
    """
    async with unscoped_session() as session:
        removed = (
            await session.execute(text("SELECT auth.reap_expired_registrations()"))
        ).scalar_one()

    count = int(removed)
    if count:
        logger.info(
            "%s", {"event": "registrations_reaped", "removed": count}, extra={"removed": count}
        )
    return count


class WorkerSettings:
    """arq settings. Queue access is behind this one interface so the prod swap to
    Cloud Tasks (§5) touches nothing else."""

    functions: ClassVar[list[object]] = [
        ping,
        ingest_source,
        ingest_document,
        embed_document,
        sync_connection,
        extract_document_job,
    ]

    #: `run_at_startup` so a deploy clears whatever accumulated while nothing was
    #: running, rather than waiting for the next slot. `unique` is arq's default and is
    #: load-bearing once there is more than one worker: without it every replica runs
    #: the same DELETE at the same instant and they contend for the same rows.
    cron_jobs: ClassVar[list[Any]] = [
        cron(
            reap_expired_registrations,
            minute=REAP_MINUTES,
            run_at_startup=True,
            unique=True,
        )
    ]

    #: Parsed, not passed through as a string. arq's `create_pool` reads `.host`, `.port`
    #: and `.database` off this object, so assigning the raw DSN raises AttributeError the
    #: moment a worker actually starts — a failure no test caught, because the suite called
    #: the job function directly and never constructed a Worker.
    redis_settings = RedisSettings.from_dsn(os.environ.get("REDIS_URL", DEFAULT_REDIS_URL))
    max_jobs = 10
    job_timeout = 600
