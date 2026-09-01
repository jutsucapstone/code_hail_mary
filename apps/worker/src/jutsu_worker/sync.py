"""Connector sync jobs: "Sync now" becomes a source walk.

`POST /v1/me/connections/{id}/sync` enqueues a `connector.sync` row. Claiming it does
NOT fetch anything itself — it makes sure the connection has its `sources` row and
enqueues the walk, then the existing pipeline does what it always does: list, fetch,
mask, chunk, embed, extract, each stage its own durable job. One sync path for local
corpora and live providers alike; a parallel ingestion system is exactly what ADR 0012
exists to prevent.

The `sources` row is the bridge (ADR 0014's data-plane half): `system` is the
provider's ACL namespace, `config_json` names the connection and the precise provider
id. `registry.resolve_connector` reads that config back when the walk runs and builds
the provider connector with the connection's proven subject and refreshed token.

A provider without a connector class still fails honestly as `source_unavailable` and
annotates the owner's connection `sync_unavailable` — the shape the UI already knows.
"""

from __future__ import annotations

import json
import uuid

from jutsu_connectors.providers import CONNECTOR_CLASSES
from jutsu_core.providers import PROVIDERS
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_worker.registry import UnsupportedSource

__all__ = ["mark_sync_unavailable", "run_sync_job"]


async def run_sync_job(session: AsyncSession, *, job: object) -> int:
    """Transaction 2 of a sync job: ensure the source row, enqueue the walk.

    The connection is loaded under the org scope the session already carries; a payload
    naming another tenant's connection finds nothing, exactly as with forged dispatch
    messages elsewhere in this worker. Returns the number of walks enqueued (1), never
    a document count — documents are the walk's business.
    """
    from jutsu_worker.ingest import source_job_key
    from jutsu_worker.jobs import JobKind, enqueue_job, reopen_completed_job

    connection_id = uuid.UUID(str(job.payload["connection_id"]))  # type: ignore[attr-defined]
    org_id = job.org_id  # type: ignore[attr-defined]
    row = (
        await session.execute(
            text("SELECT id, provider, status FROM connections WHERE id = :id"),
            {"id": connection_id},
        )
    ).first()
    if row is None:
        raise UnsupportedSource("connection not found in this organisation")
    if row.status not in ("connected", "error"):
        raise UnsupportedSource("the connection is not in a syncable state")

    provider = PROVIDERS.get(row.provider)
    if provider is None or row.provider not in CONNECTOR_CLASSES:
        raise UnsupportedSource(f"no sync fetcher for provider '{row.provider}' yet")

    source_row = (
        await session.execute(
            text(
                "SELECT id FROM sources WHERE system = CAST(:system AS source_system) "
                "AND config_json->>'connection_id' = :cid"
            ),
            {"system": provider.acl_namespace, "cid": str(connection_id)},
        )
    ).first()
    if source_row is not None:
        source_id = source_row.id
    else:
        source_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json) "
                "VALUES (:id, :org, CAST(:system AS source_system), CAST(:config AS jsonb))"
            ),
            {
                "id": source_id,
                "org": str(org_id),
                "system": provider.acl_namespace,
                "config": json.dumps(
                    {"connection_id": str(connection_id), "provider": row.provider}
                ),
            },
        )

    key = source_job_key(org_id, source_id)
    await enqueue_job(
        session,
        org_id=org_id,
        kind=JobKind.INGEST_SOURCE,
        idempotency_key=key,
        payload={"source_id": str(source_id)},
    )
    # A finished walk holds the key; a person asking to sync again is new work on the
    # same identity, exactly like a changed document (reopen_completed_job's contract).
    await reopen_completed_job(session, idempotency_key=key)
    return 1


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
