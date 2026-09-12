"""The knowledge model written into the graph, as pure functions (§7, §10).

**Everything in the graph comes from an extraction claim, and nothing else does.**
`apps/worker` extracts claims into Postgres — each one carrying a `chunk_id` that is NOT
NULL and a quote the hallucination gate proved verbatim — and this module says what those
claims become as nodes and edges. No relationship here is inferred from co-occurrence,
from proximity, or from an LLM's opinion about the shape of an organisation: one claim,
one edge, one chunk that evidences it. "Why does JUTSU believe this relationship exists"
is answerable for every edge in the store by reading the chunk it names.

**Identifiers are derived, never generated.** A node's key and an edge's id are hashes of
the things that identify them, so re-running extraction over the same document MERGEs onto
the same node and the same edge instead of growing a second copy. Idempotence is a
property of the identifiers, not of a deduplication pass that has to remember to run.

**The graph stores identifiers and entity names. It never stores passages.**

That is the security decision this module exists to hold, and it is a deliberate narrowing
of §7's node properties. Neo4j has no row-level security (ADR 0007); Postgres does, and
`ACL_PREDICATE` lives there. So an edge carries `chunk_id`, `document_id`, offsets and a
confidence — enough to recover the evidence — and never the quote itself. A caller who
retrieves a graph edge learns nothing until those identifiers have been resolved back
through Postgres under that caller's own ACL, at which point the answer is the same one
vector search would have given. Document nodes carry no title for the same reason: a title
is content, retrieval reads it from Postgres, and a copy outside the ACL boundary would be
a second place to leak from with nothing to gain.

What *is* stored outside Postgres is the entity name — a person's name, a project's name,
a decision's statement — because a knowledge graph that cannot name its entities can
neither be traversed nor matched against a question. That exposure is real, bounded, and
recorded in ADR 0022.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from jutsu_graph.labels import NodeLabel, RelationshipType

__all__ = [
    "CLAIM_LABELS",
    "MAX_NAME_CHARS",
    "ClaimRecord",
    "DocumentRef",
    "EntityRef",
    "GraphEdge",
    "UnmappedClaim",
    "edge_id",
    "entity_key",
    "normalise_name",
    "plan_edges",
]

#: The longest entity name written to the graph. Extraction already bounds `name` at 255
#: and `summary` at 1000; a decision statement is the long one, and it is truncated here
#: rather than stored whole — the statement identifies the decision for a reader and for a
#: match, and the evidence that supports it lives in Postgres.
MAX_NAME_CHARS: Final = 512

#: Which claim type becomes which node, and how that node attaches to the document that
#: evidenced it. The direction is the spec's (§7), and the two shapes differ on purpose:
#:
#:   * A *person*, *project* or *meeting* claim says the document mentions something that
#:     exists independently of it — `(Document)-[:MENTIONS]->(entity)`.
#:   * A *decision* claim says the document is the evidence for the decision, which is the
#:     relationship §7 spells `(Decision)-[:EVIDENCED_BY]->(Document)`.
#:
#: `responsibility` maps to `Person` as well. The claim names an owner and states what
#: they own; the owner is the entity and the ownership sentence is the evidence. Inventing
#: an `ActionItem` node out of a sentence fragment would be a relationship nobody
#: extracted, which is the one thing this module exists to refuse.
#:
#: §7 lists MENTIONS as reaching `Person|Project|Topic|Client`. `Meeting` is added to that
#: set here: a meeting named in a document is mentioned by it in exactly the sense the
#: other four are, and the alternative was to drop a fifth of what extraction finds.
CLAIM_LABELS: Final[dict[str, tuple[NodeLabel, RelationshipType, bool]]] = {
    # claim type: (label, relationship, document_is_source)
    "person": (NodeLabel.PERSON, RelationshipType.MENTIONS, True),
    "responsibility": (NodeLabel.PERSON, RelationshipType.MENTIONS, True),
    "project": (NodeLabel.PROJECT, RelationshipType.MENTIONS, True),
    "meeting": (NodeLabel.MEETING, RelationshipType.MENTIONS, True),
    "decision": (NodeLabel.DECISION, RelationshipType.EVIDENCED_BY, False),
}

#: Collapses to one space; everything else about a name survives for display.
_WHITESPACE: Final = re.compile(r"\s+")

#: Dropped from the *key* only, so "Project Alpha." and "project alpha" are one entity
#: while both keep the text they were extracted with.
_PUNCTUATION: Final = re.compile(r"[^\w\s]", re.UNICODE)


class UnmappedClaim(ValueError):
    """A claim type with no place in the graph.

    Raised rather than skipped silently: extraction's `CLAIM_TYPES` and this module's
    `CLAIM_LABELS` have to agree, and the way they stop agreeing is somebody adding a
    sixth claim type and nobody noticing that a fifth of what is extracted no longer
    reaches the graph. A test pins the two lists together.
    """


def normalise_name(value: str) -> str:
    """The display form: NFKC, collapsed whitespace, trimmed, bounded.

    NFKC because the same name arrives from several source systems in several Unicode
    spellings, and two spellings of one project are two nodes that never meet.
    """
    folded = unicodedata.normalize("NFKC", value)
    return _WHITESPACE.sub(" ", folded).strip()[:MAX_NAME_CHARS]


def entity_key(label: NodeLabel, name: str) -> str:
    """The identity an entity node MERGEs on, within one organisation.

    Case-folded, punctuation-stripped, whitespace-collapsed — so "Project Alpha",
    "project alpha" and "Project Alpha." are one node — then hashed together with the
    label, so a Person and a Project of the same name cannot collide.

    **Hashed rather than stored as the normalised text**, for one reason: the key is what a
    uniqueness constraint and an index are built on, and a name can be 512 characters of
    decision statement. A fixed 32-character digest indexes predictably and carries no
    more information than the name sitting on the node beside it.

    This is deliberately NOT entity resolution. Two spellings differing by more than case
    and punctuation are two entities here; §11's `resolution_queue` is where fuzzy
    matching belongs — blocking, scoring, and a human above the threshold — not a silent
    merge hidden inside a key function.
    """
    stripped = _PUNCTUATION.sub(" ", normalise_name(name).casefold())
    collapsed = _WHITESPACE.sub(" ", stripped).strip()
    digest = hashlib.sha256(f"{label.value}\x00{collapsed}".encode()).hexdigest()
    return digest[:32]


def edge_id(
    *,
    org_id: UUID,
    relationship: RelationshipType,
    source_key: str,
    target_key: str,
    chunk_id: UUID,
) -> str:
    """The identity one evidenced relationship MERGEs on.

    **The chunk is part of the identity, and that is the whole idempotence story.** One
    edge means "this chunk evidences this relationship between these two nodes". Extract
    the same document again and every claim lands on the same edge ids, so a re-run
    updates confidence and `recorded_at` and creates nothing. Two different chunks
    supporting the same relationship stay two edges, because they are two pieces of
    evidence and collapsing them would throw one away.

    The organisation is in the hash as well. Nothing depends on that for isolation — every
    query is org-scoped by `GraphSession` — but it means an id minted in one tenant cannot
    name an edge in another even by accident.
    """
    material = f"{org_id}\x00{relationship.value}\x00{source_key}\x00{target_key}\x00{chunk_id}"
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """The Postgres document a claim was extracted from, as the graph refers to it.

    `document_id` is the row id of one *version*; `source_system` and `external_id` are the
    document's identity across versions, and they are what the node MERGEs on (migration
    001's `doc_source_id` constraint). Superseding a document therefore updates the node's
    `document_id` pointer and leaves the old version's edges where they are — pointing at
    chunks whose document now has `superseded_by` set, which the ACL fetch filters out.
    Stale graph edges resolve to nothing rather than to stale evidence.
    """

    document_id: UUID
    source_system: str
    external_id: str
    #: When the document existed in the world — `documents.created_at`. The valid-time
    #: half of §7's bitemporality: an edge extracted today from a document written in
    #: March is valid from March, not from today.
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """One row of `extraction_claims`, as the graph needs it.

    Deliberately not the ORM model. `packages/graph` depends on `jutsu-core` and the Neo4j
    driver and must not grow a Postgres dependency: the worker reads the rows and hands
    over this shape, which keeps this package testable with no database at all.
    """

    claim_id: UUID
    chunk_id: UUID
    claim_type: str
    confidence: float
    #: `payload_json.name` for a person, project or meeting; the statement for a decision.
    #: Empty means the claim named nothing and cannot become a node.
    name: str
    char_start: int
    char_end: int
    extractor_version: str
    run_id: UUID


@dataclass(frozen=True, slots=True)
class EntityRef:
    """An entity node, after a claim has been mapped onto it."""

    label: NodeLabel
    key: str
    name: str


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """One evidenced relationship, ready to be written.

    Everything on it is either an identifier or a number. The quote that justified it is
    identified by `chunk_id` plus the offsets and is never copied here — see the module
    docstring.
    """

    id: str
    relationship: RelationshipType
    entity: EntityRef
    #: True when the document is the *source* of the edge — `(Document)-[:MENTIONS]->(e)`.
    #: False for `(Decision)-[:EVIDENCED_BY]->(Document)`.
    document_is_source: bool
    claim_id: UUID
    chunk_id: UUID
    claim_type: str
    confidence: float
    char_start: int
    char_end: int
    extractor_version: str
    run_id: UUID


def plan_edges(
    *, org_id: UUID, document: DocumentRef, claims: list[ClaimRecord]
) -> list[GraphEdge]:
    """Turn claims into the edges they evidence. Pure: nothing here touches a database.

    A claim whose `name` is empty after normalisation is dropped rather than written as an
    anonymous node — the model is told to leave `name` out when the passage does not give
    one, and a node keyed on the empty string would collect every nameless claim in an
    organisation into a single meaningless hub.

    Duplicate edges within one call are collapsed by id, keeping the first. Two claims of
    the same type naming the same entity from the same chunk are one fact extracted twice,
    and MERGE would collapse them anyway — doing it here makes the write count honest and
    the report reproducible.
    """
    planned: dict[str, GraphEdge] = {}

    for claim in claims:
        mapping = CLAIM_LABELS.get(claim.claim_type)
        if mapping is None:
            raise UnmappedClaim(
                f"{claim.claim_type!r} has no node in the graph. Extraction's CLAIM_TYPES "
                "and CLAIM_LABELS must agree, or a claim type stops reaching the graph "
                "with nothing at all failing."
            )
        label, relationship, document_is_source = mapping

        name = normalise_name(claim.name)
        if not name:
            continue

        key = entity_key(label, name)
        source_key, target_key = (
            (str(document.document_id), key)
            if document_is_source
            else (key, str(document.document_id))
        )
        identifier = edge_id(
            org_id=org_id,
            relationship=relationship,
            source_key=source_key,
            target_key=target_key,
            chunk_id=claim.chunk_id,
        )
        if identifier in planned:
            continue

        planned[identifier] = GraphEdge(
            id=identifier,
            relationship=relationship,
            entity=EntityRef(label=label, key=key, name=name),
            document_is_source=document_is_source,
            claim_id=claim.claim_id,
            chunk_id=claim.chunk_id,
            claim_type=claim.claim_type,
            confidence=claim.confidence,
            char_start=claim.char_start,
            char_end=claim.char_end,
            extractor_version=claim.extractor_version,
            run_id=claim.run_id,
        )

    return list(planned.values())
