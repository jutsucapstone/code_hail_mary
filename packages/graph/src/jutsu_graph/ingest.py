"""Writing extracted claims into the graph, idempotently (§7, §10).

One entry point, `sync_document`, and one promise: **running it twice over the same
claims writes the same graph.** That is not achieved by checking what is already there —
a read-then-write race between two workers would defeat that — but by deriving every
identifier from the thing it identifies (`knowledge.entity_key`, `knowledge.edge_id`) so
that every write is a `MERGE` onto a key that cannot drift.

**Nothing is ever deleted here.** A re-extraction that no longer supports an edge does not
remove it; it closes the edge's validity interval with `valid_to`, exactly as
`temporal.supersede` does, and the edge stays in the store for `as_of` to find. §7 is
explicit that superseding never deletes, and a graph that quietly dropped edges would make
every historical answer silently different from the one given last week.

**The label and the relationship type are the only things interpolated**, and both come
from `labels.py`. Cypher cannot parameterise either (that is what `labels.py` exists for),
so edges are grouped by their `(label, relationship, direction)` and one statement is run
per group with the rest of the payload arriving through `UNWIND` as bound parameters. A
group is at most five statements, because `CLAIM_LABELS` has five entries.

**Write sessions only.** Every statement here contains `MERGE` or `SET`, so a caller who
opens a `read_session` by mistake is refused by `GraphSession` rather than silently
writing nothing.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from jutsu_graph.driver import GraphSession
from jutsu_graph.knowledge import DocumentRef, GraphEdge
from jutsu_graph.labels import NodeLabel, RelationshipType
from jutsu_graph.temporal import RECORDED_AT, VALID_FROM, VALID_TO

__all__ = ["MAX_EDGES_PER_DOCUMENT", "GraphSyncReport", "sync_document"]

logger = logging.getLogger("jutsu.graph.ingest")

#: A ceiling on one document's contribution to the graph.
#:
#: Extraction is already bounded — one window of at most `MAX_WINDOW_CHARS` per document —
#: so this is not the control that keeps the graph small. It is the control that keeps a
#: *pathological* document (a model that emitted a thousand claims, a future extractor with
#: a wider window) from turning one job into an unbounded write transaction. Claims past
#: the ceiling are dropped and the report says how many, because a silent truncation here
#: would look exactly like a document with nothing in it.
MAX_EDGES_PER_DOCUMENT: Final = 500


@dataclass(frozen=True, slots=True)
class GraphSyncReport:
    """What one document's sync did. Counts and identifiers only — safe to log."""

    edges_written: int
    entities_written: int
    edges_superseded: int
    dropped_over_limit: int


#: The document node. MERGEd on the document's identity *across versions* — see
#: `DocumentRef` — with `document_id` updated to point at the current version.
_DOCUMENT: Final = (
    "MERGE (d:Document {org_id: $org_id, source_system: $source_system, "
    "external_id: $external_id}) "
    "ON CREATE SET d.id = $document_id, d.created_at = $created_at "
    "SET d.document_id = $document_id, d.updated_at = $recorded_at "
    "RETURN d.document_id AS document_id"
)


def _entity_and_edge(
    label: NodeLabel, relationship: RelationshipType, *, document_is_source: bool
) -> str:
    """One statement for one `(label, relationship, direction)` group.

    The two interpolated fragments are enum members; every other value is bound. The
    pattern is written out twice rather than assembled from a direction variable, because
    a Cypher arrow built by string concatenation is precisely the kind of cleverness that
    survives review and then points the wrong way.
    """
    edge = (
        f"MERGE (d)-[r:{relationship.value} {{id: e.id}}]->(n) "
        if document_is_source
        else f"MERGE (n)-[r:{relationship.value} {{id: e.id}}]->(d) "
    )
    return (
        "MATCH (d:Document {org_id: $org_id, source_system: $source_system, "
        "external_id: $external_id}) "
        "UNWIND $edges AS e "
        f"MERGE (n:{label.value} {{org_id: $org_id, key: e.key}}) "
        "ON CREATE SET n.id = e.key, n.created_at = $recorded_at "
        "SET n.name = e.name, n.name_lower = e.name_lower, n.updated_at = $recorded_at "
        + edge
        + "ON CREATE SET "
        f"r.org_id = $org_id, r.{VALID_FROM} = $valid_from, r.{VALID_TO} = null, "
        "r.created_at = $recorded_at "
        f"SET r.{RECORDED_AT} = $recorded_at, r.{VALID_TO} = null, "
        "r.document_id = $document_id, r.chunk_id = e.chunk_id, r.claim_id = e.claim_id, "
        "r.claim_type = e.claim_type, r.confidence = e.confidence, "
        "r.char_start = e.char_start, r.char_end = e.char_end, "
        "r.extractor_version = e.extractor_version, r.run_id = e.run_id "
        "RETURN count(r) AS written, count(DISTINCT n) AS entities"
    )


#: Closes every still-open edge this document evidenced that the current run did not
#: re-assert. `valid_to` is set; nothing is deleted. Matching is on the edge's own
#: `document_id` and `org_id`, so it cannot reach another document's edges or another
#: tenant's — and the session binds `$org_id` regardless.
_SUPERSEDE: Final = (
    "MATCH ()-[r]->() "
    "WHERE r.org_id = $org_id AND r.document_id = $document_id "
    f"AND r.{VALID_TO} IS NULL AND NOT r.id IN $kept "
    f"SET r.{VALID_TO} = $recorded_at "
    "RETURN count(r) AS closed"
)


async def sync_document(
    session: GraphSession,
    *,
    document: DocumentRef,
    edges: Sequence[GraphEdge],
    recorded_at: datetime,
) -> GraphSyncReport:
    """Write one document's evidenced relationships into the graph.

    `recorded_at` is transaction time — when JUTSU learned this — and the document's own
    `created_at` is valid time, when the fact was true in the world. Passing one for both
    is the bitemporal mistake §7 exists to prevent, so they arrive as two values from two
    sources rather than as one `now()` used twice.

    Runs inside the caller's transaction. A failure anywhere rolls the whole document's
    contribution back, which is what makes the job safely retryable: there is no state
    between "none of this document's edges" and "all of them".
    """
    kept = list(edges[:MAX_EDGES_PER_DOCUMENT])
    dropped = len(edges) - len(kept)

    await session.run(
        _DOCUMENT,
        source_system=document.source_system,
        external_id=document.external_id,
        document_id=str(document.document_id),
        created_at=document.created_at,
        recorded_at=recorded_at,
    )

    grouped: dict[tuple[NodeLabel, RelationshipType, bool], list[GraphEdge]] = defaultdict(list)
    for edge in kept:
        grouped[(edge.entity.label, edge.relationship, edge.document_is_source)].append(edge)

    written = 0
    entities = 0
    for (label, relationship, document_is_source), group in grouped.items():
        rows = await session.run(
            _entity_and_edge(label, relationship, document_is_source=document_is_source),
            source_system=document.source_system,
            external_id=document.external_id,
            document_id=str(document.document_id),
            valid_from=document.created_at,
            recorded_at=recorded_at,
            edges=[
                {
                    "id": edge.id,
                    "key": edge.entity.key,
                    "name": edge.entity.name,
                    "name_lower": edge.entity.name.casefold(),
                    "chunk_id": str(edge.chunk_id),
                    "claim_id": str(edge.claim_id),
                    "claim_type": edge.claim_type,
                    "confidence": float(edge.confidence),
                    "char_start": int(edge.char_start),
                    "char_end": int(edge.char_end),
                    "extractor_version": edge.extractor_version,
                    "run_id": str(edge.run_id),
                }
                for edge in group
            ],
        )
        if rows:
            written += int(rows[0]["written"])
            entities += int(rows[0]["entities"])

    closed_rows = await session.run(
        _SUPERSEDE,
        document_id=str(document.document_id),
        kept=[edge.id for edge in kept],
        recorded_at=recorded_at,
    )
    superseded = int(closed_rows[0]["closed"]) if closed_rows else 0

    # Identifiers and counts. No entity name, no quote, no document title — §4.9 holds
    # here exactly as it does in retrieval, and an entity name is personal data as often
    # as not.
    logger.info(
        "%s",
        {
            "event": "graph_document_synced",
            "org_id": str(session.org_id),
            "document_id": str(document.document_id),
            "edges": written,
            "entities": entities,
            "superseded": superseded,
            "dropped": dropped,
        },
    )
    return GraphSyncReport(
        edges_written=written,
        entities_written=entities,
        edges_superseded=superseded,
        dropped_over_limit=dropped,
    )
