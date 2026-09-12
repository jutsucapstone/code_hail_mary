"""GraphRAG against a real Neo4j: writing, re-writing, traversing and not leaking.

Everything here runs against the live store for the reason the rest of this suite does —
MERGE semantics, uniqueness constraints, relationship indexes and the transaction timeout
are the things under test, and a fake would agree with every assertion while proving none
of them.

Two properties carry the most weight:

  * **Idempotence.** Syncing one document twice must leave the graph identical. That is
    what makes the job safely retryable, and it is the first thing to break if an
    identifier ever stops being derived from the thing it identifies.
  * **Tenancy.** Neo4j has no row-level security, so `$org_id` on every query is the whole
    mechanism (ADR 0007). Every test here writes into two organisations and then asserts
    that a session scoped to one sees nothing of the other.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from graph_support import (  # noqa: F401 — imported so pytest registers the fixtures
    GraphFixture,
    graph_fixture,
    graph_settings_fixture,
)
from jutsu_graph.driver import (
    _WRITE_CLAUSES,
    GraphSettings,
    UnscopedQuery,
    WriteInReadSession,
    close_driver,
    read_session,
    write_session,
)
from jutsu_graph.health import GraphStatus, probe, reset_probe_cache
from jutsu_graph.ingest import MAX_EDGES_PER_DOCUMENT, sync_document
from jutsu_graph.knowledge import ClaimRecord, DocumentRef, plan_edges
from jutsu_graph.labels import NodeLabel
from jutsu_graph.retrieval import (
    _EXPAND,
    _seed_statement,
    candidate_names,
    expand_from_documents,
    graph_candidates,
    seed_from_names,
)

NOW = datetime(2026, 4, 1, tzinfo=UTC)


def document(external_id: str = "doc-1") -> DocumentRef:
    return DocumentRef(
        document_id=uuid.uuid4(),
        source_system="local",
        external_id=external_id,
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )


def claim(claim_type: str, name: str, *, chunk_id: uuid.UUID | None = None) -> ClaimRecord:
    return ClaimRecord(
        claim_id=uuid.uuid4(),
        chunk_id=chunk_id or uuid.uuid4(),
        claim_type=claim_type,
        confidence=0.8,
        name=name,
        char_start=0,
        char_end=len(name),
        extractor_version="1.0.0",
        run_id=uuid.uuid4(),
    )


async def write(
    fixture: GraphFixture, org_id: uuid.UUID, doc: DocumentRef, claims: list[ClaimRecord]
) -> None:
    edges = plan_edges(org_id=org_id, document=doc, claims=claims)
    async with write_session(org_id, settings=fixture.settings) as session:
        await sync_document(session, document=doc, edges=edges, recorded_at=NOW)


async def count(fixture: GraphFixture, org_id: uuid.UUID) -> tuple[int, int]:
    """Nodes and open relationships in one organisation."""
    async with read_session(org_id, settings=fixture.settings) as session:
        nodes = await session.run("MATCH (n) WHERE n.org_id = $org_id RETURN count(n) AS n")
        rels = await session.run(
            "MATCH ()-[r]->() WHERE r.org_id = $org_id AND r.valid_to IS NULL RETURN count(r) AS n"
        )
    return int(nodes[0]["n"]), int(rels[0]["n"])


class TestWriting:
    async def test_a_claim_becomes_an_entity_and_an_evidenced_edge(
        self, graph: GraphFixture
    ) -> None:
        doc = document()
        mention = claim("project", "Project Alpha")

        await write(graph, graph.org_a, doc, [mention])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                "MATCH (d:Document)-[r:MENTIONS]->(p:Project) "
                "WHERE d.org_id = $org_id AND p.org_id = $org_id "
                "RETURN p.name AS name, r.chunk_id AS chunk_id, r.claim_id AS claim_id, "
                "r.confidence AS confidence, r.valid_to AS valid_to"
            )

        assert len(rows) == 1
        assert rows[0]["name"] == "Project Alpha"
        # The provenance, which is what makes the edge answerable rather than asserted.
        assert rows[0]["chunk_id"] == str(mention.chunk_id)
        assert rows[0]["claim_id"] == str(mention.claim_id)
        assert rows[0]["confidence"] == pytest.approx(0.8)
        # Open interval: this fact is current until something supersedes it.
        assert rows[0]["valid_to"] is None

    async def test_the_passage_itself_is_never_stored(self, graph: GraphFixture) -> None:
        # The security property of the whole design: Neo4j holds identifiers and entity
        # names, and every passage stays in Postgres behind ACL_PREDICATE. A `quote`
        # property here would be tenant content in a store with no row-level security.
        await write(graph, graph.org_a, document(), [claim("project", "Project Alpha")])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                "MATCH ()-[r:MENTIONS]->() WHERE r.org_id = $org_id RETURN keys(r) AS keys"
            )
            docs = await session.run(
                "MATCH (d:Document) WHERE d.org_id = $org_id RETURN keys(d) AS keys"
            )

        assert "quote" not in rows[0]["keys"]
        assert "text" not in rows[0]["keys"]
        # Not even the title: retrieval reads it from Postgres under the caller's ACL.
        assert "title" not in docs[0]["keys"]

    async def test_syncing_the_same_document_twice_changes_nothing(
        self, graph: GraphFixture
    ) -> None:
        doc = document()
        claims = [claim("person", "Ada Lovelace"), claim("project", "Project Alpha")]

        await write(graph, graph.org_a, doc, claims)
        before = await count(graph, graph.org_a)
        await write(graph, graph.org_a, doc, claims)

        assert await count(graph, graph.org_a) == before

    async def test_two_documents_naming_one_project_share_its_node(
        self, graph: GraphFixture
    ) -> None:
        # The property that makes traversal worth anything: entities are shared, so a
        # question about a project can reach every document that mentions it.
        await write(graph, graph.org_a, document("doc-1"), [claim("project", "Project Alpha")])
        await write(graph, graph.org_a, document("doc-2"), [claim("project", "project alpha")])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                "MATCH (p:Project) WHERE p.org_id = $org_id RETURN count(p) AS n"
            )

        assert int(rows[0]["n"]) == 1

    async def test_a_reextraction_that_drops_a_claim_supersedes_its_edge(
        self, graph: GraphFixture
    ) -> None:
        doc = document()
        kept = claim("project", "Project Alpha")
        dropped = claim("person", "Ada Lovelace")

        await write(graph, graph.org_a, doc, [kept, dropped])
        await write(graph, graph.org_a, doc, [kept])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                "MATCH ()-[r]-() WHERE r.org_id = $org_id "
                "RETURN r.claim_type AS claim_type, r.valid_to AS valid_to"
            )

        states = {row["claim_type"]: row["valid_to"] for row in rows}
        assert states["project"] is None
        # Closed, not deleted: §7 is explicit that superseding never removes, and `as_of`
        # before this instant must still find it.
        assert states["person"] is not None

    async def test_a_document_with_no_claims_closes_what_it_used_to_assert(
        self, graph: GraphFixture
    ) -> None:
        doc = document()
        await write(graph, graph.org_a, doc, [claim("project", "Project Alpha")])

        await write(graph, graph.org_a, doc, [])

        _, open_edges = await count(graph, graph.org_a)
        assert open_edges == 0

    async def test_one_document_cannot_write_unbounded_edges(self, graph: GraphFixture) -> None:
        doc = document()
        claims = [claim("project", f"Project {n}") for n in range(MAX_EDGES_PER_DOCUMENT + 5)]
        edges = plan_edges(org_id=graph.org_a, document=doc, claims=claims)

        async with write_session(graph.org_a, settings=graph.settings) as session:
            report = await sync_document(session, document=doc, edges=edges, recorded_at=NOW)

        assert report.dropped_over_limit == 5
        # Reported rather than silent: a truncation nobody counts looks exactly like a
        # document that contained nothing.
        assert report.edges_written == MAX_EDGES_PER_DOCUMENT


class TestTenancy:
    async def test_one_organisation_never_sees_another_s_entities(
        self, graph: GraphFixture
    ) -> None:
        await write(graph, graph.org_a, document(), [claim("project", "Project Alpha")])

        assert await count(graph, graph.org_b) == (0, 0)

    async def test_the_same_entity_name_in_two_tenants_is_two_nodes(
        self, graph: GraphFixture
    ) -> None:
        # The compound (org_id, key) constraint proving itself: a shared name must not
        # become a shared node, or one tenant's traversal would arrive in another's
        # documents.
        await write(graph, graph.org_a, document("a"), [claim("project", "Project Alpha")])
        await write(graph, graph.org_b, document("b"), [claim("project", "Project Alpha")])

        assert (await count(graph, graph.org_a))[0] == (await count(graph, graph.org_b))[0] == 2


class TestTraversal:
    async def test_expansion_reaches_a_document_through_a_shared_entity(
        self, graph: GraphFixture
    ) -> None:
        # §12's graph expansion: one hop out of a vector hit, one hop back into another
        # document. This is the passage a vector search would not have ranked.
        first, second = document("doc-1"), document("doc-2")
        neighbour = claim("project", "Project Alpha")
        await write(graph, graph.org_a, first, [claim("project", "Project Alpha")])
        await write(graph, graph.org_a, second, [neighbour])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            found = await expand_from_documents(session, document_ids=[first.document_id])

        assert [candidate.chunk_id for candidate in found] == [neighbour.chunk_id]
        assert found[0].document_id == second.document_id
        assert found[0].hops[0].entity_name == "Project Alpha"

    async def test_expansion_does_not_return_the_seed_s_own_chunks(
        self, graph: GraphFixture
    ) -> None:
        # The vector half already has those. Returning them would inflate the graph's
        # apparent contribution and waste the fusion's budget on rows it already holds.
        only = document()
        await write(graph, graph.org_a, only, [claim("project", "Project Alpha")])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            found = await expand_from_documents(session, document_ids=[only.document_id])

        assert found == []

    async def test_a_question_naming_an_entity_finds_its_passages(
        self, graph: GraphFixture
    ) -> None:
        doc = document()
        mention = claim("project", "Project Alpha")
        await write(graph, graph.org_a, doc, [mention])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            found = await seed_from_names(
                session, names=candidate_names("who works on Project Alpha?")
            )

        assert [candidate.chunk_id for candidate in found] == [mention.chunk_id]

    async def test_a_superseded_edge_is_not_traversed(self, graph: GraphFixture) -> None:
        # A relationship the latest extraction no longer supports must stop answering
        # questions, without being deleted.
        doc = document()
        await write(graph, graph.org_a, doc, [claim("project", "Project Alpha")])
        await write(graph, graph.org_a, doc, [])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            found = await seed_from_names(session, names=["project alpha"])

        assert found == []

    async def test_expansion_does_not_traverse_a_superseded_edge(self, graph: GraphFixture) -> None:
        """The same rule on the other path, which had no test until a mutation said so.

        `seed_from_names` and `_EXPAND` each carry their own `valid_to IS NULL`, and only
        the first was pinned: replacing the expansion's temporal filter with `true` left
        the suite green. A superseded edge reached through expansion is the same defect —
        an answer citing a relationship the evidence no longer supports.
        """
        first, second = document("doc-1"), document("doc-2")
        await write(graph, graph.org_a, first, [claim("project", "Project Alpha")])
        await write(graph, graph.org_a, second, [claim("project", "Project Alpha")])
        # The neighbour re-extracts and no longer mentions the project.
        await write(graph, graph.org_a, second, [])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            found = await expand_from_documents(session, document_ids=[first.document_id])

        assert found == []

    async def test_a_traversal_is_scoped_to_its_own_tenant(self, graph: GraphFixture) -> None:
        # Same entity name, two tenants, and the traversal runs in the wrong one. This is
        # the test that would fail if `$org_id` were ever dropped from a template.
        their_doc = document("theirs")
        await write(graph, graph.org_b, their_doc, [claim("project", "Project Alpha")])
        await write(graph, graph.org_b, document("theirs-2"), [claim("project", "Project Alpha")])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            seeded = await seed_from_names(session, names=["project alpha"])
            expanded = await expand_from_documents(session, document_ids=[their_doc.document_id])

        assert seeded == []
        assert expanded == []

    async def test_both_paths_merge_into_one_ranked_set(self, graph: GraphFixture) -> None:
        first, second = document("doc-1"), document("doc-2")
        await write(graph, graph.org_a, first, [claim("project", "Project Alpha")])
        await write(graph, graph.org_a, second, [claim("project", "Project Alpha")])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            found = await graph_candidates(
                session, query="tell me about Project Alpha", document_ids=[first.document_id]
            )

        # Both documents' passages, each exactly once, in a deterministic order.
        assert len({candidate.chunk_id for candidate in found}) == len(found) == 2
        assert list(found) == sorted(found, key=lambda c: (-c.confidence, str(c.chunk_id)))

    async def test_a_limit_is_honoured(self, graph: GraphFixture) -> None:
        doc = document()
        await write(graph, graph.org_a, doc, [claim("project", f"Project {n}") for n in range(10)])

        async with read_session(graph.org_a, settings=graph.settings) as session:
            found = await seed_from_names(
                session, names=[f"project {n}" for n in range(10)], limit=3
            )

        assert len(found) <= 3


class TestQuerySafety:
    """Static properties of the templates. No database — these read the strings."""

    def test_every_element_a_template_binds_is_organisation_scoped(self) -> None:
        """Every node and every relationship, named one at a time.

        `"$org_id" in statement` was the first spelling of this and it is far too weak: a
        mutation run deleted the scope from four of the expansion's five elements and
        this test stayed green, because the one remaining conjunct kept the phrase in the
        string. It stayed green for a second reason too — with correctly written data the
        traversal cannot leave its tenant anyway, since every edge was written through an
        org-scoped session. That is exactly why these conjuncts are a belt: they are what
        holds when the data is *not* correct, after a restore, a bad backfill or a writer
        nobody has reviewed yet. A belt no test can see is a belt somebody removes.
        """
        for element in ("d", "e", "d2", "r1", "r2"):
            assert f"{element}.org_id = $org_id" in _EXPAND, element

        for label in NodeLabel:
            statement = _seed_statement(label)
            for element in ("e", "d", "r"):
                assert f"{element}.org_id = $org_id" in statement, (label, element)

    def test_no_retrieval_template_can_write(self) -> None:
        # The regex `read_session` enforces, applied to the text a reviewer reads.
        for statement in (_EXPAND, *(_seed_statement(label) for label in NodeLabel)):
            assert _WRITE_CLAUSES.search(statement) is None

    def test_no_template_has_a_variable_length_pattern(self) -> None:
        # "Maximum depth" is a property of the text rather than a parameter: there is no
        # `*1..n` anywhere, so no traversal can be widened by changing a number.
        for statement in (_EXPAND, *(_seed_statement(label) for label in NodeLabel)):
            assert "*" not in statement

    def test_every_template_is_bounded(self) -> None:
        for statement in (_EXPAND, *(_seed_statement(label) for label in NodeLabel)):
            assert "LIMIT $limit" in statement

    async def test_a_write_is_refused_in_a_read_session(self, graph: GraphFixture) -> None:
        async with read_session(graph.org_a, settings=graph.settings) as session:
            with pytest.raises(WriteInReadSession):
                await session.run("MATCH (n) WHERE n.org_id = $org_id SET n.name = 'x'")

    async def test_an_unscoped_query_is_refused(self, graph: GraphFixture) -> None:
        async with read_session(graph.org_a, settings=graph.settings) as session:
            with pytest.raises(UnscopedQuery):
                await session.run("MATCH (n:Project) RETURN n")

    async def test_a_caller_cannot_supply_its_own_organisation(self, graph: GraphFixture) -> None:
        # The one that matters most: a parameter map must not be able to smuggle another
        # tenant's id past the session's binding.
        async with read_session(graph.org_a, settings=graph.settings) as session:
            with pytest.raises(UnscopedQuery):
                await session.run(
                    "MATCH (n) WHERE n.org_id = $org_id RETURN n", org_id=str(graph.org_b)
                )


class TestHealthProbe:
    """What `/readyz` asks, and the three answers it can get.

    The driver is a process-wide singleton, so each of these closes it explicitly: a
    probe against a dead address would otherwise reuse a healthy pool and report `ok`,
    and a probe that created a dead pool would poison every test after it.
    """

    async def test_an_unconfigured_deployment_reports_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reset_probe_cache()
        monkeypatch.delenv("NEO4J_URI", raising=False)

        assert await probe() is GraphStatus.NOT_CONFIGURED

    async def test_a_live_graph_reports_ok(self, graph: GraphFixture) -> None:
        reset_probe_cache()

        assert await probe(settings=graph.settings) is GraphStatus.OK

    async def test_an_unreachable_graph_reports_degraded_rather_than_raising(
        self, graph_settings: GraphSettings
    ) -> None:
        # A readiness probe reports a state; it does not raise one. And it must not wait
        # for the driver's own connection timeout, which is far longer than any platform
        # gives a health endpoint.
        reset_probe_cache()
        await close_driver()
        dead = GraphSettings(uri="bolt://127.0.0.1:1", user="neo4j", password="unused")

        try:
            assert await probe(settings=dead, timeout_s=1.0) is GraphStatus.DEGRADED
        finally:
            reset_probe_cache()
            await close_driver()

    def test_the_credential_is_never_in_a_settings_repr(self) -> None:
        # `GraphSettings` is constructed on every probe and a failure logs the object it
        # was holding. §4.9 has no carve-out for a repr.
        settings = GraphSettings(uri="bolt://host:7687", user="neo4j", password="hunter2")

        assert "hunter2" not in repr(settings)
        assert "redacted" in repr(settings)


class TestCandidateNames:
    """Pure: turning a question into phrases that could name an entity."""

    def test_phrases_are_lowercased_word_ngrams(self) -> None:
        assert "project alpha" in candidate_names("Who owns Project Alpha?")

    def test_very_short_words_are_not_matched(self) -> None:
        # "of", "to", "a" — every one of them is a hub nobody asked about.
        assert "of" not in candidate_names("head of engineering")

    def test_the_phrase_list_is_bounded_by_the_question(self) -> None:
        names = candidate_names("a b c d e f g h")

        assert len(names) < 60
