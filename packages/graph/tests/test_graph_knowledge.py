"""The claim-to-graph mapping, as pure functions (ADR 0022).

No database at all: everything here is about what an extraction claim *becomes*, which is
decided before anything is written and is the part that has to be right for the write to
be idempotent. The live-store behaviour is `test_graphrag_store.py`.

The assertions worth reading twice are the identity ones. A node key and an edge id are
derived from the things they identify, and that derivation is the entire reason running
extraction twice does not double the graph.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from jutsu_graph.knowledge import (
    CLAIM_LABELS,
    ClaimRecord,
    DocumentRef,
    UnmappedClaim,
    edge_id,
    entity_key,
    normalise_name,
    plan_edges,
)
from jutsu_graph.labels import NodeLabel, RelationshipType

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")


def document(document_id: uuid.UUID | None = None) -> DocumentRef:
    return DocumentRef(
        document_id=document_id or uuid.uuid4(),
        source_system="local",
        external_id="doc-1",
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )


def claim(
    claim_type: str = "project",
    name: str = "Project Alpha",
    *,
    chunk_id: uuid.UUID | None = None,
    confidence: float = 0.9,
) -> ClaimRecord:
    return ClaimRecord(
        claim_id=uuid.uuid4(),
        chunk_id=chunk_id or uuid.uuid4(),
        claim_type=claim_type,
        confidence=confidence,
        name=name,
        char_start=0,
        char_end=len(name),
        extractor_version="1.0.0",
        run_id=uuid.uuid4(),
    )


class TestNames:
    def test_whitespace_and_unicode_are_normalised(self) -> None:
        # The same project arrives from four source systems in four spellings. Two
        # spellings of one name are two nodes that never meet.
        assert normalise_name("  Project   Alpha \n") == "Project Alpha"

    def test_a_name_is_bounded(self) -> None:
        # A decision "name" is a whole statement; the store keeps an identifier, not an
        # essay.
        assert len(normalise_name("x" * 5000)) == 512


class TestEntityKeys:
    def test_case_and_punctuation_do_not_make_a_second_entity(self) -> None:
        assert entity_key(NodeLabel.PROJECT, "Project Alpha") == entity_key(
            NodeLabel.PROJECT, "project alpha."
        )

    def test_two_labels_of_the_same_name_do_not_collide(self) -> None:
        # A person and a project can share a name, and a key that ignored the label would
        # silently merge them into one node with two kinds of edge.
        assert entity_key(NodeLabel.PERSON, "Alpha") != entity_key(NodeLabel.PROJECT, "Alpha")

    def test_a_key_is_stable_across_calls(self) -> None:
        # The idempotence of every MERGE rests on this one line.
        assert entity_key(NodeLabel.PERSON, "Ada Lovelace") == entity_key(
            NodeLabel.PERSON, "Ada Lovelace"
        )

    def test_a_different_name_is_a_different_entity(self) -> None:
        # Deliberately NOT fuzzy: resolving "Ada" and "Ada Lovelace" to one person is
        # §11's job, with a human above the threshold, not a key function's.
        assert entity_key(NodeLabel.PERSON, "Ada") != entity_key(NodeLabel.PERSON, "Ada Lovelace")


class TestEdgeIdentity:
    def test_the_same_claim_produces_the_same_edge(self) -> None:
        chunk = uuid.uuid4()
        first = edge_id(
            org_id=ORG,
            relationship=RelationshipType.MENTIONS,
            source_key="doc",
            target_key="entity",
            chunk_id=chunk,
        )
        second = edge_id(
            org_id=ORG,
            relationship=RelationshipType.MENTIONS,
            source_key="doc",
            target_key="entity",
            chunk_id=chunk,
        )

        assert first == second

    def test_two_chunks_evidencing_the_same_relationship_are_two_edges(self) -> None:
        # Two passages supporting one relationship are two pieces of evidence. Collapsing
        # them would throw one away, and the provenance is the whole point.
        #
        # Written out rather than unpacked from a shared dict: `edge_id` takes four
        # different types, and a `**common` of `dict[str, object]` is exactly what mypy
        # cannot check — which would make this test the one place a wrong argument type
        # could reach that function unnoticed.
        first = edge_id(
            org_id=ORG,
            relationship=RelationshipType.MENTIONS,
            source_key="doc",
            target_key="entity",
            chunk_id=uuid.uuid4(),
        )
        second = edge_id(
            org_id=ORG,
            relationship=RelationshipType.MENTIONS,
            source_key="doc",
            target_key="entity",
            chunk_id=uuid.uuid4(),
        )

        assert first != second

    def test_the_organisation_is_part_of_the_identity(self) -> None:
        chunk = uuid.uuid4()
        mine = edge_id(
            org_id=ORG,
            relationship=RelationshipType.MENTIONS,
            source_key="doc",
            target_key="entity",
            chunk_id=chunk,
        )
        theirs = edge_id(
            org_id=OTHER_ORG,
            relationship=RelationshipType.MENTIONS,
            source_key="doc",
            target_key="entity",
            chunk_id=chunk,
        )

        assert mine != theirs


class TestPlanning:
    def test_a_person_claim_is_mentioned_by_the_document(self) -> None:
        edges = plan_edges(org_id=ORG, document=document(), claims=[claim("person", "Ada")])

        assert len(edges) == 1
        assert edges[0].relationship is RelationshipType.MENTIONS
        assert edges[0].entity.label is NodeLabel.PERSON
        # §7's direction: `(Document)-[:MENTIONS]->(Person)`.
        assert edges[0].document_is_source is True

    def test_a_decision_is_evidenced_by_the_document(self) -> None:
        # The other direction, and the reason the two shapes are written out separately:
        # `(Decision)-[:EVIDENCED_BY]->(Document)`.
        edges = plan_edges(org_id=ORG, document=document(), claims=[claim("decision", "We ship")])

        assert edges[0].relationship is RelationshipType.EVIDENCED_BY
        assert edges[0].document_is_source is False

    def test_a_responsibility_names_a_person(self) -> None:
        edges = plan_edges(
            org_id=ORG, document=document(), claims=[claim("responsibility", "Grace Hopper")]
        )

        assert edges[0].entity.label is NodeLabel.PERSON

    def test_every_extraction_claim_type_has_a_mapping(self) -> None:
        # The failure this catches: somebody adds a sixth claim type to extraction and a
        # fifth of what the model finds stops reaching the graph with nothing failing.
        # The worker-side test pins the two lists to each other; this one pins the shape.
        for claim_type, (label, relationship, _) in CLAIM_LABELS.items():
            assert isinstance(label, NodeLabel), claim_type
            assert isinstance(relationship, RelationshipType), claim_type

    def test_an_unknown_claim_type_raises_rather_than_being_skipped(self) -> None:
        with pytest.raises(UnmappedClaim):
            plan_edges(org_id=ORG, document=document(), claims=[claim("sentiment", "happy")])

    def test_a_nameless_claim_becomes_no_node(self) -> None:
        # A node keyed on the empty string would collect every nameless claim in the
        # organisation into one meaningless hub that every traversal runs through.
        edges = plan_edges(org_id=ORG, document=document(), claims=[claim("person", "   ")])

        assert edges == []

    def test_the_same_claims_plan_the_same_edges(self) -> None:
        doc = document()
        claims = [claim("person", "Ada"), claim("project", "Alpha")]

        assert [e.id for e in plan_edges(org_id=ORG, document=doc, claims=claims)] == [
            e.id for e in plan_edges(org_id=ORG, document=doc, claims=claims)
        ]

    def test_one_fact_extracted_twice_from_one_chunk_is_one_edge(self) -> None:
        chunk = uuid.uuid4()
        claims = [
            claim("project", "Project Alpha", chunk_id=chunk),
            claim("project", "project alpha", chunk_id=chunk),
        ]

        assert len(plan_edges(org_id=ORG, document=document(), claims=claims)) == 1

    def test_provenance_travels_with_every_edge(self) -> None:
        # "Why does JUTSU believe this relationship exists" has to be answerable from the
        # edge alone, and the answer is a chunk id — never a copy of the passage.
        source = claim("person", "Ada")
        edges = plan_edges(org_id=ORG, document=document(), claims=[source])

        assert edges[0].chunk_id == source.chunk_id
        assert edges[0].claim_id == source.claim_id
        assert edges[0].confidence == source.confidence
        assert edges[0].extractor_version == "1.0.0"
