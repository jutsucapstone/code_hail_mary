"""Hybrid retrieval: what the graph may add, and what it may never add (ADR 0022).

Two questions, and the second one is the whole reason this file exists.

**Does the graph improve the answer?** A passage the vector search did not rank, reached
through an entity two documents share, arrives in the evidence and is cited like any
other.

**Can the graph widen what somebody sees?** No, and every test below tries. The graph is a
store with no row-level security that knows nothing about who is asking; it returns chunk
identifiers, and `fetch_evidence_many` resolves them under `ACL_PREDICATE` against the
caller's own principals. So the adversarial cases here are not exotic — they are the
ordinary output of a traversal over a corpus where entities are shared and grants are not.

The graph half is a scripted `GraphReader` throughout. That is deliberate: the failures
that matter most are a store that times out, raises, or returns another tenant's chunk,
and none of those can be asked of a real Neo4j on demand. `packages/graph` tests the real
traversal against the real database.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from jutsu_api.answers import synthesise_answer
from jutsu_api.graphrag import (
    RetrievalMode,
    RetrievalPath,
    graphrag_enabled,
    retrieve,
)
from jutsu_graph.retrieval import GraphCandidate, GraphHop
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DIM = 768


def vec(*leading: float) -> list[float]:
    return [*leading, *([0.0] * (DIM - len(leading)))]


def literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


# ------------------------------------------------------------------ the scripted graph


class ScriptedGraph:
    """A `GraphReader` that returns what it is told — slowly, or by raising.

    Records every call, which is how "the graph was never asked" is asserted. That is a
    real property rather than a detail: the flag being off has to mean no connection
    attempt, not a connection whose result is discarded.
    """

    def __init__(
        self,
        results: Sequence[GraphCandidate] = (),
        *,
        error: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.results = list(results)
        self.error = error
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []

    async def candidates(
        self, *, org_id: uuid.UUID, query: str, document_ids: Sequence[uuid.UUID], limit: int
    ) -> Sequence[GraphCandidate]:
        self.calls.append(
            {"org_id": org_id, "query": query, "document_ids": list(document_ids), "limit": limit}
        )
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return self.results


def graph_reader(
    chunk_ids: Sequence[uuid.UUID] = (),
    *,
    error: Exception | None = None,
    delay_s: float = 0.0,
) -> ScriptedGraph:
    """A reader that offers exactly these chunks, in this order."""
    return ScriptedGraph(
        [
            GraphCandidate(
                chunk_id=chunk_id,
                document_id=uuid.uuid4(),
                confidence=0.9,
                hops=(GraphHop("Project", "Project Alpha", "MENTIONS"),),
            )
            for chunk_id in chunk_ids
        ],
        error=error,
        delay_s=delay_s,
    )


# ------------------------------------------------------------------ the corpus


async def scope(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
    )


async def make_org(session: AsyncSession, label: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    await scope(session, org_id)
    await session.execute(
        text("INSERT INTO orgs (id, name) VALUES (:i, :n)"), {"i": org_id, "n": label}
    )
    return org_id


async def make_user(
    session: AsyncSession, org_id: uuid.UUID, label: str, *, subject: str | None = None
) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, org_id, email, status) VALUES (:i,:o,:e,'active')"),
        {"i": user_id, "o": org_id, "e": f"{label}@example.com"},
    )
    if subject is not None:
        await session.execute(
            text(
                "INSERT INTO source_identities (org_id, user_id, source_system, subject) "
                "VALUES (:o,:u,'local',:s)"
            ),
            {"o": org_id, "u": user_id, "s": subject},
        )
    return user_id


async def make_source(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID:
    source_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:i,:o,'local','{}'::jsonb)"
        ),
        {"i": source_id, "o": org_id},
    )
    return source_id


async def make_document(
    session: AsyncSession,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    title: str,
    grants: list[tuple[str, str]],
    embedding: list[float],
) -> uuid.UUID:
    """One document with one chunk. Returns the chunk id — the unit everything here ranks."""
    doc_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
            "acl_hash, body_original, body_masked, created_at) "
            "VALUES (:i,:o,:s,:e,:t,'h','a','original','masked',now())"
        ),
        {"i": doc_id, "o": org_id, "s": source_id, "e": str(doc_id), "t": title},
    )
    for principal_type, principal_id in grants:
        await session.execute(
            text(
                "INSERT INTO document_acl (document_id, principal_type, principal_id, org_id) "
                "VALUES (:d,:pt,:pi,:o)"
            ),
            {"d": doc_id, "pt": principal_type, "pi": principal_id, "o": org_id},
        )
    chunk_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO chunks (id, document_id, org_id, ordinal, text, char_start, char_end, "
            "token_count, embedding) "
            "VALUES (:i,:d,:o,0,:x,0,12,3,CAST(:v AS vector))"
        ),
        {"i": chunk_id, "d": doc_id, "o": org_id, "x": f"{title} chunk", "v": literal(embedding)},
    )
    return chunk_id


@pytest.fixture
async def world(db_session: AsyncSession) -> dict[str, Any]:
    """One tenant Ada can read part of, and a second tenant she cannot read at all.

    * `near`      granted to Ada; closest to the query vector
    * `orgwide`   granted to the organisation; second closest
    * `distant`   granted to the organisation; orthogonal to the query, so the vector
                  half never reaches it at a small `k` — which makes it the only passage
                  that can prove the graph added something rather than reordered it
    * `secret`    granted to somebody else — the graph will offer it anyway
    * `elsewhere` in another tenant, granted to the *same principal string* Ada holds
    """
    alpha = await make_org(db_session, "alpha")
    source = await make_source(db_session, alpha)
    ada = await make_user(db_session, alpha, "ada", subject="ada@example.com")

    near = await make_document(
        db_session,
        alpha,
        source,
        title="near",
        grants=[("user", "local:ada@example.com")],
        embedding=vec(1.0),
    )
    orgwide = await make_document(
        db_session,
        alpha,
        source,
        title="orgwide",
        grants=[("org", str(alpha))],
        embedding=vec(0.6, 0.8),
    )
    distant = await make_document(
        db_session,
        alpha,
        source,
        title="distant",
        grants=[("org", str(alpha))],
        embedding=vec(0.0, 0.0, 1.0),
    )
    secret = await make_document(
        db_session,
        alpha,
        source,
        title="secret",
        grants=[("user", "local:eve@example.com")],
        embedding=vec(0.99),
    )

    beta = await make_org(db_session, "beta")
    beta_source = await make_source(db_session, beta)
    elsewhere = await make_document(
        db_session,
        beta,
        beta_source,
        title="elsewhere",
        grants=[("user", "local:ada@example.com")],
        embedding=vec(1.0),
    )

    await scope(db_session, alpha)
    return {
        "alpha": alpha,
        "ada": ada,
        "near": near,
        "orgwide": orgwide,
        "distant": distant,
        "secret": secret,
        "elsewhere": elsewhere,
    }


@pytest.fixture(autouse=True)
def graph_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag on, and connection details present, unless a test says otherwise.

    Both are needed: the feature is off by default and stays off without a `NEO4J_URI`,
    which is the rollout contract — so a test of the *enabled* behaviour has to say so.
    """
    monkeypatch.setenv("GRAPHRAG_ENABLED", "true")
    monkeypatch.setenv("NEO4J_URI", "bolt://graph.invalid:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "unused-by-the-scripted-reader")


async def hybrid(
    session: AsyncSession, world: dict[str, Any], reader: ScriptedGraph, **kwargs: Any
) -> Any:
    return await retrieve(
        session,
        org_id=world["alpha"],
        user_id=world["ada"],
        query="what happened on Project Alpha",
        query_vector=vec(1.0),
        k=kwargs.pop("k", 10),
        reader=reader,
        **kwargs,
    )


class TestTheACLGate:
    """The invariant: an unauthorized source never reaches the model."""

    async def test_a_graph_candidate_the_caller_may_not_read_is_dropped(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        # `secret` is in this tenant, is nearer the query vector than anything Ada may
        # read, and the graph offers it. Vector search already refuses it; this asserts
        # the second path refuses it too.
        reader = graph_reader([world["secret"]])

        outcome = await hybrid(db_session, world, reader)

        assert world["secret"] not in {item.chunk_id for item in outcome.evidence}
        assert outcome.report.graph_candidates == 1
        assert outcome.report.graph_authorized == 0
        assert outcome.report.graph_dropped == 1

    async def test_a_graph_candidate_from_another_tenant_is_dropped(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        # The graph is scoped by `$org_id`, so this should be unreachable — and it is
        # still refused here, because tenant isolation must not depend on a store that
        # cannot enforce it. Beta granted this document to the same principal string Ada
        # holds, so only the tenant scope keeps it out.
        reader = graph_reader([world["elsewhere"]])

        outcome = await hybrid(db_session, world, reader)

        assert world["elsewhere"] not in {item.chunk_id for item in outcome.evidence}
        assert outcome.report.graph_dropped == 1

    async def test_a_mixed_candidate_set_keeps_only_what_is_authorized(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        # The ordinary output of a real traversal: entities are shared, grants are not.
        reader = graph_reader([world["secret"], world["orgwide"], world["elsewhere"]])

        outcome = await hybrid(db_session, world, reader)

        returned = {item.chunk_id for item in outcome.evidence}
        assert world["orgwide"] in returned
        assert world["secret"] not in returned
        assert world["elsewhere"] not in returned
        assert outcome.report.graph_authorized == 1
        assert outcome.report.graph_dropped == 2

    async def test_an_unknown_chunk_id_is_dropped_silently(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        # A graph written by an older job can name a chunk that no longer exists. It must
        # be indistinguishable from one the caller may not read — and neither may raise.
        reader = graph_reader([uuid.uuid4()])

        outcome = await hybrid(db_session, world, reader)

        assert outcome.report.graph_authorized == 0
        assert outcome.evidence  # the vector half is unaffected


class TestWhatTheGraphAdds:
    async def test_an_authorized_graph_candidate_reaches_the_evidence(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        # `distant` is orthogonal to the query vector, so at k=2 the vector half ranks
        # `near` and `orgwide` and never reaches it. It arrives because two documents
        # share an entity — which is the entire claim GraphRAG makes.
        #
        # It also displaces `orgwide`: a graph candidate at rank 1 outranks a vector
        # candidate at rank 2 under RRF, and the result is still bounded by k.
        reader = graph_reader([world["distant"]])

        outcome = await hybrid(db_session, world, reader, k=2)

        assert world["distant"] in {item.chunk_id for item in outcome.evidence}
        assert outcome.report.graph_added == 1
        assert outcome.report.path is RetrievalPath.HYBRID
        assert len(outcome.evidence) == 2

    async def test_a_chunk_both_paths_found_is_returned_once(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        reader = graph_reader([world["near"]])

        outcome = await hybrid(db_session, world, reader)

        assert [item.chunk_id for item in outcome.evidence].count(world["near"]) == 1
        # Found by both, so it is not something the graph *added*.
        assert outcome.report.graph_added == 0

    async def test_agreement_between_the_paths_ranks_a_passage_first(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        # RRF's contribution, and the reason fusion is by rank rather than by score:
        # `orgwide` is second on the vector side and first on the graph side, so the
        # agreement between two paths overtakes `near`, which only one path ranked first.
        reader = graph_reader([world["orgwide"]])

        outcome = await hybrid(db_session, world, reader)

        assert outcome.evidence[0].chunk_id == world["orgwide"]

    async def test_the_result_is_bounded_by_k(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        reader = graph_reader([world["orgwide"]])

        outcome = await hybrid(db_session, world, reader, k=1)

        assert len(outcome.evidence) == 1

    async def test_a_graph_passage_carries_a_measured_similarity(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        # Not 1.0. A graph-contributed passage sitting in a list of cosine similarities
        # claiming a perfect match would be a lie the UI renders as a number.
        reader = graph_reader([world["distant"]])

        outcome = await hybrid(db_session, world, reader, k=2)

        added = next(item for item in outcome.evidence if item.chunk_id == world["distant"])
        assert 0.0 <= added.score < 0.5

    async def test_the_graph_is_seeded_from_the_vector_hits(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        # §12's expansion is "1-hop neighbours of vector hits", so the vector half has to
        # run first and hand over what it found.
        reader = graph_reader([])

        await hybrid(db_session, world, reader)

        assert reader.calls[0]["document_ids"]
        assert reader.calls[0]["org_id"] == world["alpha"]


class TestFallback:
    """Scenarios 2, 3 and 4 of the fallback contract. All five land in one place."""

    async def test_a_timeout_falls_back_to_vector(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        reader = graph_reader([world["orgwide"]], delay_s=5.0)

        outcome = await hybrid(db_session, world, reader)

        assert outcome.report.path is RetrievalPath.VECTOR
        assert outcome.report.fallback_reason == "timeout"
        assert outcome.evidence  # and the answer still has its evidence

    async def test_an_unreachable_graph_falls_back_to_vector(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        from neo4j.exceptions import ServiceUnavailable

        # `type: ignore` because the driver leaves its exception constructors unannotated
        # and this build is strict — the same accommodation `test_failure_classification`
        # makes, and for the same third-party reason.
        unreachable = ServiceUnavailable("no route to host")  # type: ignore[no-untyped-call]
        reader = graph_reader([world["orgwide"]], error=unreachable)

        outcome = await hybrid(db_session, world, reader)

        assert outcome.report.path is RetrievalPath.VECTOR
        assert outcome.report.fallback_reason == "error"

    async def test_an_unexpected_exception_falls_back_rather_than_failing_the_request(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        # The class of failure nobody predicted. A retrieval layer that let this escape
        # would turn an optional dependency into a 500 on a question pgvector can answer.
        reader = graph_reader([world["orgwide"]], error=RuntimeError("something new"))

        outcome = await hybrid(db_session, world, reader)

        assert outcome.report.path is RetrievalPath.VECTOR
        assert outcome.report.fallback_reason == "error"

    async def test_the_flag_off_means_the_graph_is_never_asked(
        self, db_session: AsyncSession, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRAPHRAG_ENABLED", "false")
        reader = graph_reader([world["orgwide"]])

        outcome = await hybrid(db_session, world, reader)

        assert outcome.report.fallback_reason == "disabled"
        assert reader.calls == []

    async def test_no_connection_details_means_the_graph_is_never_asked(
        self, db_session: AsyncSession, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NEO4J_URI", raising=False)
        reader = graph_reader([world["orgwide"]])

        outcome = await hybrid(db_session, world, reader)

        assert outcome.report.fallback_reason == "not_configured"
        assert reader.calls == []

    async def test_an_explicit_vector_request_is_honoured(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        reader = graph_reader([world["orgwide"]])

        outcome = await hybrid(db_session, world, reader, mode=RetrievalMode.VECTOR)

        assert outcome.report.path is RetrievalPath.VECTOR
        assert reader.calls == []

    async def test_a_paginated_request_takes_the_vector_path(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        # `next_cursor` is a keyset over the vector ordering, and a fused list is not in
        # that order. Rather than return a page that can skip rows, the graph half is
        # skipped and the reason says so.
        reader = graph_reader([world["orgwide"]])

        outcome = await hybrid(
            db_session, world, reader, after=(0.99, uuid.uuid4()), mode=RetrievalMode.HYBRID
        )

        assert outcome.report.fallback_reason == "paginated"
        assert reader.calls == []

    async def test_the_fallback_returns_exactly_what_vector_search_returned(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        # The floor. Whatever the graph does, the caller is never worse off than they
        # were before GraphRAG existed.
        broken = graph_reader([world["orgwide"]], error=RuntimeError("down"))
        disabled = graph_reader([world["orgwide"]])

        with_error = await hybrid(db_session, world, broken)
        with_vector = await hybrid(db_session, world, disabled, mode=RetrievalMode.VECTOR)

        assert [item.chunk_id for item in with_error.evidence] == [
            item.chunk_id for item in with_vector.evidence
        ]
        assert [item.chunk_id for item in with_error.evidence] == [
            item.chunk_id for item in with_error.page.items
        ]


class TestCitations:
    async def test_a_graph_contributed_passage_cites_its_document(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """The graph is an index over evidence, never a source of it (§12, ADR 0022).

        So a passage the graph found is cited exactly like one the vector search found:
        by chunk, document, title and source system. Nothing in a citation names a node,
        a relationship or anything else that exists only in Neo4j — those are not
        addressable, not fetchable through `/v1/evidence/{chunk_id}`, and not something a
        reader could ever open.
        """
        reader = graph_reader([world["distant"]])
        outcome = await hybrid(db_session, world, reader, k=2)

        marker = next(
            index
            for index, item in enumerate(outcome.evidence, start=1)
            if item.chunk_id == world["distant"]
        )

        class Scripted:
            async def complete(self, *, system: str, prompt: str) -> str:
                return f"The work continued through the autumn [{marker}]."

        answer = await synthesise_answer(
            Scripted(), question="what happened", evidence=list(outcome.evidence)
        )

        assert answer.insufficient_evidence is False
        assert [citation.chunk_id for citation in answer.citations] == [str(world["distant"])]
        assert answer.citations[0].document_title == "distant"
        assert answer.citations[0].source_system == "local"


class TestTheFlag:
    def test_the_feature_is_off_unless_explicitly_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GRAPHRAG_ENABLED", raising=False)

        assert graphrag_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_the_spellings_of_yes(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("GRAPHRAG_ENABLED", value)

        assert graphrag_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe", "enabled?"])
    def test_anything_else_is_off(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        # A flag that reads `GRAPHRAG_ENABLED=maybe` as enabled is worse than no flag.
        monkeypatch.setenv("GRAPHRAG_ENABLED", value)

        assert graphrag_enabled() is False
