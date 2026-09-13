"""Extraction claims into the graph, as a job of its own (ADR 0022).

**A separate job kind, for the same reason `embed.document` is separate from
`ingest.document`:** a failure at one stage must never force another to run again.
Writing the graph inline at the end of extraction would mean an unreachable Neo4j failing
an extraction that had already succeeded and been paid for, and the retry would call the
model again to produce claims Postgres already holds.

So the pipeline gains one link at the end and nothing else changes:

    ingest.document → embed.document → extract.document → graph.document

**Nothing upstream depends on this link existing.** With no `NEO4J_URI` the job is never
enqueued (the same shape of gate extraction uses for its provider keys), and ingestion,
embedding
and extraction behave exactly as they did before. With Neo4j configured but down, the job
fails, is retried with backoff, and every other stage is untouched — documents keep
arriving, chunks keep embedding, and pgvector retrieval keeps answering.

**Postgres is the source of truth and this is a projection of it.** Every node and edge is
derived from rows that already exist; losing the graph entirely loses nothing that cannot
be rebuilt by re-running these jobs, which is why they can be dropped, retried and
re-ordered freely.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from jutsu_graph.driver import MissingGraphSettings, get_graph_settings, write_session
from jutsu_graph.ingest import GraphSyncReport, sync_document
from jutsu_graph.knowledge import ClaimRecord, DocumentRef, plan_edges
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "MAX_CLAIMS_PER_DOCUMENT",
    "graph_configured",
    "graph_sync_job_key",
    "sync_document_graph",
]

logger = logging.getLogger("jutsu.worker.graph")

#: A bound on one job's read. Extraction writes one window's worth of claims per document,
#: so this is not the number that keeps the graph small — it is the one that keeps a single
#: pathological document from reading an unbounded result set into memory.
MAX_CLAIMS_PER_DOCUMENT: Final = 1000

#: Which payload field names the entity, per claim type.
#:
#: `person`, `project` and `responsibility` name somebody or something: the extractor is
#: told to put it in `name`, and a claim without one names nothing that could be a node.
#: `decision` and `meeting` are described rather than named — the prompt asks for a
#: `summary` and says nothing about `name` — so the summary is the statement that
#: identifies them, with `name` accepted first in case a future prompt supplies one.
_NAME_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "person": ("name",),
    "responsibility": ("name",),
    "project": ("name",),
    "decision": ("summary", "name"),
    "meeting": ("summary", "name"),
}

#: The document's identity, which is what the graph node MERGEs on. RLS scopes this to the
#: caller's organisation; the `org_id` conjunct is the belt over that policy, exactly as
#: every other query in this package carries one.
_DOCUMENT: Final = (
    "SELECT d.external_id, d.created_at, CAST(s.system AS text) AS source_system "
    "FROM documents d JOIN sources s ON s.id = d.source_id "
    "WHERE d.id = :document_id"
)

#: The latest finished extraction run's claims for one document.
#:
#: `extraction_runs` carries no `document_id` — a run is created per document but the
#: column does not exist — so "this document's latest run" is derived through the claims'
#: chunks, which is the read model CLAUDE.md describes. Superseded claims are excluded:
#: §4.4 supersedes rather than overwriting, and a projection that replayed both versions
#: would write an edge for a claim that has been replaced.
_CLAIMS: Final = (
    "SELECT c.id AS claim_id, c.chunk_id, c.claim_type, c.confidence, c.payload_json, "
    "c.run_id "
    "FROM extraction_claims c "
    "JOIN chunks ch ON ch.id = c.chunk_id "
    "WHERE ch.document_id = :document_id "
    "AND c.superseded_by IS NULL "
    "AND c.run_id = ("
    "  SELECT c2.run_id FROM extraction_claims c2 "
    "  JOIN chunks ch2 ON ch2.id = c2.chunk_id "
    "  JOIN extraction_runs r ON r.id = c2.run_id "
    "  WHERE ch2.document_id = :document_id AND r.finished_at IS NOT NULL "
    "  ORDER BY r.finished_at DESC, r.started_at DESC LIMIT 1"
    ") "
    "ORDER BY c.id "
    "LIMIT :limit"
)


def graph_configured() -> bool:
    """Whether this deployment has a graph to write to.

    Checked at **enqueue** time as well as here, for the reason extraction's equivalent is:
    queueing jobs whose only possible outcome is failure fills the dead-letter view with
    noise about a fact the operator already knows.
    """
    try:
        get_graph_settings()
    except MissingGraphSettings:
        return False
    return True


def graph_sync_job_key(org_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """One graph sync per document.

    Keyed on the document rather than on the extraction run, so a re-extraction reopens
    this job instead of queueing a second one — the graph write is idempotent, and the
    thing being projected is "this document's current claims", which has one answer at a
    time.
    """
    return f"graph.document:{org_id}:{document_id}"


def _name_of(claim_type: str, payload: dict[str, Any]) -> str:
    for field in _NAME_FIELDS.get(claim_type, ("name",)):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return ""


async def sync_document_graph(
    session: AsyncSession, *, org_id: uuid.UUID, document_id: uuid.UUID
) -> GraphSyncReport | None:
    """Project one document's latest claims into the graph. `None` if the document is gone.

    Reads from Postgres in the caller's transaction and writes to Neo4j in a transaction
    of its own — two stores, two transactions, and no pretence that they commit together.
    The consequence is stated rather than hidden: a crash between them leaves Postgres
    correct and the graph one document behind, and the retry rewrites the same edges onto
    the same ids. That is exactly the failure this whole design is arranged to make
    harmless (ADR 0022).
    """
    row = (await session.execute(text(_DOCUMENT), {"document_id": str(document_id)})).first()
    if row is None:
        # The document was superseded and its row replaced, or it never existed in this
        # tenant. Either way there is nothing to project and nothing to retry — a missing
        # document is not a failure, exactly as `DocumentGone` is not one upstream.
        logger.info(
            "%s",
            {
                "event": "graph_sync_document_absent",
                "org_id": str(org_id),
                "document_id": str(document_id),
            },
        )
        return None

    document = DocumentRef(
        document_id=document_id,
        source_system=str(row.source_system),
        external_id=str(row.external_id),
        created_at=row.created_at,
    )

    claim_rows = (
        await session.execute(
            text(_CLAIMS),
            {"document_id": str(document_id), "limit": MAX_CLAIMS_PER_DOCUMENT},
        )
    ).all()

    claims = [
        ClaimRecord(
            claim_id=uuid.UUID(str(claim.claim_id)),
            chunk_id=uuid.UUID(str(claim.chunk_id)),
            claim_type=str(claim.claim_type),
            confidence=float(claim.confidence),
            name=_name_of(str(claim.claim_type), dict(claim.payload_json or {})),
            char_start=int((claim.payload_json or {}).get("char_start") or 0),
            char_end=int((claim.payload_json or {}).get("char_end") or 0),
            extractor_version=str((claim.payload_json or {}).get("extractor_version") or ""),
            run_id=uuid.UUID(str(claim.run_id)),
        )
        for claim in claim_rows
    ]

    edges = plan_edges(org_id=org_id, document=document, claims=claims)

    # An empty edge list is not a no-op: it MERGEs the document node and closes every
    # edge the previous run asserted. A re-extraction that now finds nothing is a
    # statement about the document, and leaving last week's relationships open would make
    # the graph disagree with the evidence it claims to be derived from.
    async with write_session(org_id) as graph:
        report = await sync_document(
            graph, document=document, edges=edges, recorded_at=datetime.now(UTC)
        )

    logger.info(
        "%s",
        {
            "event": "graph_sync_completed",
            "org_id": str(org_id),
            "document_id": str(document_id),
            "claims": len(claims),
            "edges": report.edges_written,
            "entities": report.entities_written,
            "superseded": report.edges_superseded,
            "dropped": report.dropped_over_limit,
        },
    )
    return report
