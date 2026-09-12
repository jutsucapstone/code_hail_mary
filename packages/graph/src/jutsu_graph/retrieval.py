"""Graph retrieval: relationships in, chunk identifiers out (§12).

**This module returns identifiers, never evidence.** Every candidate it produces is a
`chunk_id` and a `document_id` that came off an edge's provenance — nothing else. The
caller takes those identifiers to Postgres and fetches the chunks through
`ACL_PREDICATE`, which is where the decision about what this person may read is made and
the only place it is ever made. A graph candidate the caller may not read simply fails to
come back from that fetch.

That division is the reason this module can be as permissive as it likes about traversal:
it is not an authorization surface. Neo4j knows which documents mention which people; it
does not know, and is never asked, who is allowed to read them.

**Every query is a constant in this file.** There is no query builder, no LLM-authored
Cypher, and no path by which caller text reaches the query *text* — a question arrives as
a bound list of strings in `$names`. §22's generated-Cypher path will need a procedure
allowlist on top of `labels.py`; it is not built here, and nothing here would let one in
through the back door.

**The traversal is bounded by shape, not by a depth counter.** `_EXPAND` is written as two
fixed hops — document, entity, document — with no variable-length pattern anywhere in the
module, so "maximum depth" is a property of the text a reviewer can read rather than a
parameter somebody can raise. On top of that: every statement carries a `LIMIT`, the
session carries a server-enforced statement timeout, and the seed list is bounded by the
vector search that produced it.

**Read sessions only.** Nothing here writes, and `GraphSession` refuses a write clause in
a read session, so that is enforced rather than intended.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from jutsu_graph.driver import GraphSession
from jutsu_graph.knowledge import CLAIM_LABELS
from jutsu_graph.labels import NodeLabel
from jutsu_graph.temporal import VALID_TO

__all__ = [
    "DEFAULT_GRAPH_LIMIT",
    "ENTITY_LABELS",
    "MAX_GRAPH_LIMIT",
    "MAX_NAME_WORDS",
    "MAX_SEED_DOCUMENTS",
    "GraphCandidate",
    "GraphHop",
    "candidate_names",
    "expand_from_documents",
    "graph_candidates",
    "seed_from_names",
]

logger = logging.getLogger("jutsu.graph.retrieval")

#: How many candidates one traversal may return. Matches `jutsu_retrieval.DEFAULT_K`, so
#: the two halves of the hybrid arrive at comparable size and neither swamps the fusion.
DEFAULT_GRAPH_LIMIT: Final = 30

#: The ceiling a caller may ask for. Nothing above this is useful: the fusion truncates,
#: and an unbounded traversal on a dense entity is the one way this layer could hurt a
#: request path that is otherwise bounded by an index.
MAX_GRAPH_LIMIT: Final = 100

#: How many vector hits are used as traversal seeds. The whole top-k is more seeds than
#: the expansion needs and multiplies the work of the two-hop match for candidates the
#: fusion will not reach.
MAX_SEED_DOCUMENTS: Final = 10

#: The longest phrase matched against an entity name. Names in this corpus are one to
#: four words; six is generous and keeps the parameter list small on a long question.
MAX_NAME_WORDS: Final = 6

#: Shortest phrase worth matching. Two characters matches initials, articles and half the
#: prepositions in English, and every one of those is a hub entity nobody asked about.
_MIN_NAME_CHARS: Final = 3

#: The labels an extracted entity can carry, derived from the claim mapping so the two
#: cannot drift. A label reaching a query comes from here, never from a caller.
ENTITY_LABELS: Final[tuple[NodeLabel, ...]] = tuple(
    dict.fromkeys(label for label, _, _ in CLAIM_LABELS.values())
)

_WORD: Final = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class GraphHop:
    """One step of the path that produced a candidate.

    Carried so an answer can explain *why* the graph offered a passage — "this document
    and that one both mention Project Alpha" — and so a diagnostic view can show the
    traversal. It is not a citation and must never be rendered as one: the citation is the
    chunk, which is fetched from Postgres under the caller's ACL (§12, and ADR 0022).
    """

    entity_label: str
    entity_name: str
    relationship: str


@dataclass(frozen=True, slots=True)
class GraphCandidate:
    """One chunk the graph suggests, with the reason attached.

    `confidence` is the extractor's own number for the edge that produced this candidate,
    in `[0, 1]`. It ranks graph candidates against each other and nothing else — in
    particular it is never compared with a vector similarity, which is a different
    quantity in the same range. Fusion is by rank for exactly that reason (ADR 0022).
    """

    chunk_id: UUID
    document_id: UUID
    confidence: float
    hops: tuple[GraphHop, ...]


def candidate_names(query: str, *, max_words: int = MAX_NAME_WORDS) -> list[str]:
    """Every phrase in `query` that could name an entity, lowercased.

    Word n-grams up to `max_words`, in the order they appear, deduplicated. A question of
    twenty words yields at most a hundred or so phrases, which is a comfortable `IN` list
    against an indexed property.

    **Deterministic, and deliberately not a model call.** An LLM asked to pick entity
    names out of a question is one more place to hallucinate, one more paid call on the
    request path, and untestable without a fixture; matching the text against names the
    graph actually holds cannot invent an entity that was never extracted. The cost is
    that a question asking about "the Alpha project" does not match an entity stored as
    "Project Alpha" — recall this path does not have, rather than precision it fakes.
    """
    folded = unicodedata.normalize("NFKC", query).casefold()
    words = _WORD.findall(folded)
    names: dict[str, None] = {}
    for size in range(1, max(1, max_words) + 1):
        for start in range(0, len(words) - size + 1):
            phrase = " ".join(words[start : start + size])
            if len(phrase) >= _MIN_NAME_CHARS:
                names[phrase] = None
    return list(names)


#: Two fixed hops: a seed document, an entity it is connected to, and another document
#: connected to that entity. The chunk that comes back is the one that evidenced the
#: *second* edge, which is the passage that mentions the entity in the other document —
#: exactly the passage a reader would want next.
#:
#: Both relationships are matched undirected, because the two shapes point opposite ways:
#: `(Document)-[:MENTIONS]->(entity)` and `(Decision)-[:EVIDENCED_BY]->(Document)`.
#: Direction carries meaning for a reader and none for this traversal.
#:
#: `org_id` is asserted on all five elements. The session binds it and cannot be widened,
#: and a missing conjunct here would still be a bug worth catching, so the test asserts
#: the count.
_EXPAND: Final = (
    "MATCH (d:Document)-[r1]-(e)-[r2]-(d2:Document) "
    "WHERE d.org_id = $org_id AND e.org_id = $org_id AND d2.org_id = $org_id "
    "AND r1.org_id = $org_id AND r2.org_id = $org_id "
    "AND d.document_id IN $seeds "
    f"AND r1.{VALID_TO} IS NULL AND r2.{VALID_TO} IS NULL "
    "AND d2.document_id <> d.document_id "
    "AND r2.chunk_id IS NOT NULL "
    "RETURN r2.chunk_id AS chunk_id, r2.document_id AS document_id, "
    "labels(e)[0] AS entity_label, e.name AS entity_name, type(r2) AS relationship, "
    "coalesce(r2.confidence, 0.0) AS confidence "
    "ORDER BY confidence DESC, chunk_id ASC "
    "LIMIT $limit"
)


def _seed_statement(label: NodeLabel) -> str:
    """Entities this question names, and the passages that evidenced them.

    One statement per label so the `(org_id, name_lower)` index is usable: Neo4j indexes
    are per-label, and a label-less `MATCH (e) WHERE e.name_lower IN $names` cannot touch
    one. The label is an allowlist member; every other value is bound.
    """
    return (
        f"MATCH (e:{label.value})-[r]-(d:Document) "
        "WHERE e.org_id = $org_id AND d.org_id = $org_id AND r.org_id = $org_id "
        "AND e.name_lower IN $names "
        f"AND r.{VALID_TO} IS NULL "
        "AND r.chunk_id IS NOT NULL "
        "RETURN r.chunk_id AS chunk_id, r.document_id AS document_id, "
        f"'{label.value}' AS entity_label, e.name AS entity_name, type(r) AS relationship, "
        "coalesce(r.confidence, 0.0) AS confidence "
        "ORDER BY confidence DESC, chunk_id ASC "
        "LIMIT $limit"
    )


def _collect(rows: Sequence[object], into: dict[UUID, GraphCandidate]) -> None:
    """Merge result rows into the candidate set, best-confidence-wins per chunk.

    A chunk reached twice — by two entities, or by both paths — keeps its highest
    confidence and accumulates the hop that found it, so the explanation names every
    reason rather than the last one.
    """
    for row in rows:
        record = row  # neo4j.Record, indexable by column name
        raw_chunk = record["chunk_id"]  # type: ignore[index]
        raw_document = record["document_id"]  # type: ignore[index]
        if raw_chunk is None or raw_document is None:
            continue
        try:
            chunk_id = UUID(str(raw_chunk))
            document_id = UUID(str(raw_document))
        except ValueError:
            # A malformed identifier on an edge is data, not a crash: the graph is
            # written by a job that can be older than this code. Skipping it costs one
            # candidate; raising would cost the whole request its graph half.
            continue

        hop = GraphHop(
            entity_label=str(record["entity_label"] or ""),  # type: ignore[index]
            entity_name=str(record["entity_name"] or ""),  # type: ignore[index]
            relationship=str(record["relationship"] or ""),  # type: ignore[index]
        )
        confidence = float(record["confidence"] or 0.0)  # type: ignore[index]

        existing = into.get(chunk_id)
        if existing is None:
            into[chunk_id] = GraphCandidate(
                chunk_id=chunk_id,
                document_id=document_id,
                confidence=confidence,
                hops=(hop,),
            )
        elif hop not in existing.hops:
            into[chunk_id] = GraphCandidate(
                chunk_id=existing.chunk_id,
                document_id=existing.document_id,
                confidence=max(existing.confidence, confidence),
                hops=(*existing.hops, hop),
            )


async def expand_from_documents(
    session: GraphSession,
    *,
    document_ids: Sequence[UUID],
    limit: int = DEFAULT_GRAPH_LIMIT,
) -> list[GraphCandidate]:
    """§12's graph expansion: the one-entity neighbourhood of documents already retrieved.

    Empty seeds return nothing without touching the database — a vector search that found
    nothing has nothing to expand from, and a query with an empty `IN` list is a scan
    waiting to be written by somebody tidying this away.
    """
    seeds = [str(document_id) for document_id in document_ids[:MAX_SEED_DOCUMENTS]]
    if not seeds:
        return []

    found: dict[UUID, GraphCandidate] = {}
    rows = await session.run(_EXPAND, seeds=seeds, limit=min(limit, MAX_GRAPH_LIMIT))
    _collect(rows, found)
    return list(found.values())


async def seed_from_names(
    session: GraphSession,
    *,
    names: Sequence[str],
    limit: int = DEFAULT_GRAPH_LIMIT,
) -> list[GraphCandidate]:
    """Entities the question names, and the passages that evidenced them.

    This is the half that answers a relationship question the vector index is weak at —
    "who worked on Project Alpha" retrieves the passages that put a person and that
    project on the record, rather than the passages whose prose is most similar to the
    question.
    """
    if not names:
        return []

    found: dict[UUID, GraphCandidate] = {}
    bounded = min(limit, MAX_GRAPH_LIMIT)
    for label in ENTITY_LABELS:
        rows = await session.run(_seed_statement(label), names=list(names), limit=bounded)
        _collect(rows, found)
    return list(found.values())


async def graph_candidates(
    session: GraphSession,
    *,
    query: str,
    document_ids: Sequence[UUID],
    limit: int = DEFAULT_GRAPH_LIMIT,
) -> list[GraphCandidate]:
    """Both paths, merged and ranked. The entry point a retrieval layer calls.

    Ordering is by the extractor's confidence and then by chunk id — a total order, so the
    same graph and the same question produce the same ranking on every call. Fusion
    downstream is by rank, and a ranking that is not deterministic makes a hybrid result
    set that changes under a reader for no reason they can see.
    """
    started = time.monotonic()
    bounded = min(limit, MAX_GRAPH_LIMIT)

    merged: dict[UUID, GraphCandidate] = {}
    for candidate in await expand_from_documents(session, document_ids=document_ids, limit=bounded):
        merged[candidate.chunk_id] = candidate
    for candidate in await seed_from_names(session, names=candidate_names(query), limit=bounded):
        existing = merged.get(candidate.chunk_id)
        if existing is None:
            merged[candidate.chunk_id] = candidate
        else:
            merged[candidate.chunk_id] = GraphCandidate(
                chunk_id=existing.chunk_id,
                document_id=existing.document_id,
                confidence=max(existing.confidence, candidate.confidence),
                hops=(*existing.hops, *(h for h in candidate.hops if h not in existing.hops)),
            )

    ranked = sorted(merged.values(), key=lambda c: (-c.confidence, str(c.chunk_id)))[:bounded]

    # Counts and timings. Never the question, never an entity name — the same rule
    # `jutsu_retrieval.search` follows, for the same reason: an entity name here is a
    # person's name about as often as not.
    logger.info(
        "%s",
        {
            "event": "graph_retrieval",
            "org_id": str(session.org_id),
            "seeds": min(len(document_ids), MAX_SEED_DOCUMENTS),
            "candidates": len(ranked),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return ranked
