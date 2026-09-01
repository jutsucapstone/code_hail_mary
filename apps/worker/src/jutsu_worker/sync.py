"""Connector sync jobs: the durable half of "Sync now".

`POST /v1/me/connections/{id}/sync` enqueues a `connector.sync` row in the same durable
queue ingestion uses. This module is the worker's side of that contract — and it is
honest about the platform's current reach: **no per-provider fetcher exists yet**, so a
claimed sync job fails with the same classified, non-retryable shape an unsupported
source system produces, lands in the operator's Jobs view as `source_unavailable`, and
annotates the connection so its owner sees `sync_unavailable` instead of a spinner.

That is deliberately better than the two alternatives: leaving the job `pending`
forever (a queue that lies about its backlog) or completing it as if a sync happened
(the fake success §36 forbids). When the first fetcher lands, `fetcher_for` grows its
first real entry and everything around it — claim, lease, bounded attempts, failure
classification, the connection annotation — is already in place and tested.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_worker.registry import UnsupportedSource

__all__ = ["mark_sync_unavailable", "run_sync_job"]


def fetcher_for(provider: str) -> None:
    """The provider fetcher registry. Currently empty, and says so per provider.

    Mirrors `registry.connector_for`'s stance for source systems: the absence of a
    fetcher is a permanent condition no retry changes, so the exception is the one the
    classifier already maps to a non-retryable failure.
    """
    raise UnsupportedSource(f"no sync fetcher for provider '{provider}' yet")


async def run_sync_job(session: AsyncSession, *, job: object) -> int:
    """Transaction 2 of a sync job. Raises until a fetcher exists.

    The connection is loaded under the org scope the session already carries; a payload
    naming another tenant's connection finds nothing, exactly as with forged dispatch
    messages elsewhere in this worker.
    """
    connection_id = uuid.UUID(str(job.payload["connection_id"]))  # type: ignore[attr-defined]
    row = (
        await session.execute(
            text("SELECT id, provider, status FROM connections WHERE id = :id"),
            {"id": connection_id},
        )
    ).first()
    if row is None:
        raise UnsupportedSource("connection not found in this organisation")

    fetcher_for(row.provider)
    # Unreachable until a fetcher exists; the return type documents the contract
    # (documents fetched) for when one does.
    return 0  # pragma: no cover


async def mark_sync_unavailable(session: AsyncSession, *, connection_id: uuid.UUID) -> None:
    """Annotate the connection after a failed sync, in its own transaction.

    Runs in the failure path, after the work transaction is dead — the same reason
    `record_failure` gets a fresh session. The classified kind, never an error string,
    is what the owner's UI renders (§4.9).
    """
    await session.execute(
        text(
            "UPDATE connections SET last_error_kind = 'sync_unavailable', "
            "updated_at = now() WHERE id = :id"
        ),
        {"id": connection_id},
    )
