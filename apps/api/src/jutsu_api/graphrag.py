"""Hybrid retrieval: the existing vector search, plus the graph, behind one fallback rule.

    vector search  →  graph expansion  →  ACL fetch  →  RRF fusion  →  context  →  LLM
         │                                                                         │
         └──────────────── anything above fails ───────────────────────────────────┘

**The vector search runs first and its result is the floor.** Everything the graph adds is
added to a list that is already complete and already authorized, so there is no state in
which a graph failure leaves the caller with less than they had before GraphRAG existed.
That is the whole design: the fallback is not an error path that has to work, it is the
absence of an enhancement that has to be earned. `Neo4j unavailable`, `not configured`,
`slow`, `flag off`, `raised something nobody predicted` — all five land in exactly the
same place, which is `retrieval_mode=vector` with a reason attached.

**Neo4j never decides who may read anything.** The graph returns chunk identifiers; those
identifiers go to `fetch_evidence_many`, which runs `ACL_PREDICATE` in SQL against the
caller's own principals. A candidate the caller may not read does not come back, is not
counted in a way that discloses it, and never reaches the model. Postgres remains the
authority for authorization exactly as it was before (ADR 0022).

**Hybrid applies to the first page only.** `SearchPage.next_cursor` is a keyset over the
vector ordering — `(score, chunk_id)` — and a fused list is not in that order. Mixing them
would make page two skip or repeat rows, so a request carrying a cursor takes the vector
path and says so. Pagination through a fused ranking needs a cursor that describes the
fusion, which is a design, not a patch.

**What this does not do is rerank.** §12 ends with a cross-encoder over the fused
candidates, and there is no cross-encoder in this deployment; pretending otherwise by
sorting on a similarity that RRF deliberately discarded would be worse than not doing it.
The fused order is what reaches the model, truncated to `k`. Recorded as a gap rather than
quietly implied.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from jutsu_graph.driver import MissingGraphSettings, get_graph_settings, read_session
from jutsu_graph.retrieval import (
    DEFAULT_GRAPH_LIMIT,
    MAX_GRAPH_LIMIT,
    GraphCandidate,
    graph_candidates,
)
from jutsu_retrieval import Evidence, RetrievalWindow, SearchPage, reciprocal_rank_fusion
from jutsu_retrieval.evidence import fetch_evidence_many
from jutsu_retrieval.search import search_chunks
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "GRAPH_DEADLINE_S",
    "GraphReader",
    "HybridOutcome",
    "Neo4jGraphReader",
    "RetrievalMode",
    "RetrievalPath",
    "RetrievalReport",
    "get_graph_reader",
    "graphrag_enabled",
    "reset_graph_reader",
    "retrieve",
]

logger = logging.getLogger("jutsu.api.graphrag")

#: The flag that turns the graph half on. **Off unless explicitly set**, so the feature
#: ships to production dark and is enabled by a configuration change rather than by a
#: deploy — which is what makes a bad rollout a one-line revert with no image to rebuild.
_ENABLED_ENV: Final = "GRAPHRAG_ENABLED"

#: Accepted spellings of "yes". Anything else, including the empty string, is off — a
#: feature flag that reads `GRAPHRAG_ENABLED=maybe` as enabled is worse than no flag.
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})

#: The whole graph half of a request, including connecting. Deliberately small: a search
#: that would have answered in 200 ms must not wait seconds for an enhancement, and every
#: millisecond past this is one the caller pays for evidence they already had.
#:
#: `driver.DEFAULT_STATEMENT_TIMEOUT_S` bounds the server side at 30 s, which is the right
#: bound for an ingestion transaction and far too generous here. Both apply; the smaller
#: one wins in practice, and the server-side one remains as the backstop for a client that
#: is killed mid-query.
GRAPH_DEADLINE_S: Final = 2.5


class RetrievalMode(StrEnum):
    """What the caller asked for.

    `AUTO` is the default and means "use the graph if it is there" — a caller should not
    have to know the deployment's configuration to get the best answer it can give.
    `VECTOR` is an explicit opt-out, which exists for evaluation and for a caller that
    wants the cheapest path. `HYBRID` asks for the graph and still falls back: refusing to
    answer because an optional dependency is down would be the single point of failure
    this whole design exists to avoid.
    """

    AUTO = "auto"
    VECTOR = "vector"
    HYBRID = "hybrid"


class RetrievalPath(StrEnum):
    """What actually happened. Reported, never requested."""

    VECTOR = "vector"
    HYBRID = "hybrid"


class GraphReader(Protocol):
    """The graph half, behind a seam.

    A Protocol for the same reason every paid or external dependency here has one: the
    tests that matter most are the ones where this misbehaves — times out, raises, returns
    a chunk from another tenant — and none of those can be asked of a real Neo4j on
    demand.
    """

    async def candidates(
        self, *, org_id: UUID, query: str, document_ids: Sequence[UUID], limit: int
    ) -> Sequence[GraphCandidate]: ...


class Neo4jGraphReader:
    """The real traversal, in a read-only org-scoped transaction.

    Holds nothing between calls. The driver underneath is a process-wide pool owned by
    `jutsu_graph.driver`, which is where a connection's lifetime belongs.
    """

    async def candidates(
        self, *, org_id: UUID, query: str, document_ids: Sequence[UUID], limit: int
    ) -> Sequence[GraphCandidate]:
        async with read_session(org_id, timeout_s=GRAPH_DEADLINE_S) as session:
            return await graph_candidates(
                session, query=query, document_ids=document_ids, limit=limit
            )


@dataclass(frozen=True, slots=True)
class RetrievalReport:
    """How the answer was retrieved. Counts, timings and a reason — never content.

    `graph_dropped` is the number of graph candidates that did not survive the ACL fetch.
    It is safe to report *to the caller who was refused them* — they learn that the graph
    suggested something they may not read, which is a count of their own denials and
    discloses no document, no title and no identifier. It is not exposed to anyone else,
    and the log line carries the same number for exactly the same reason: an ACL filter
    with no observable effect is one nobody would notice failing open.
    """

    path: RetrievalPath
    graph_candidates: int
    graph_authorized: int
    graph_dropped: int
    #: Chunks the graph contributed that the vector search had not already found.
    graph_added: int
    graph_elapsed_ms: int
    #: Why the graph half did not run, or None when it did. One of: disabled,
    #: not_configured, paginated, unreachable, timeout, error.
    fallback_reason: str | None


@dataclass(frozen=True, slots=True)
class HybridOutcome:
    """Evidence for the answer, plus the vector page the caller's contract still needs.

    `page` is exactly what `search_chunks` returned — stats and cursor included — because
    `/v1/search` promises those and they describe the vector half, which is unchanged.
    `evidence` is what the model (or the caller) should actually use.
    """

    evidence: tuple[Evidence, ...]
    page: SearchPage
    report: RetrievalReport


def graphrag_enabled() -> bool:
    """Whether this deployment has been told to use the graph at all."""
    return os.environ.get(_ENABLED_ENV, "").strip().casefold() in _TRUTHY


def _graph_configured() -> bool:
    """Whether connection details exist. Never connects — that is the probe's job."""
    try:
        get_graph_settings()
    except MissingGraphSettings:
        return False
    return True


_reader: GraphReader | None = None


def get_graph_reader() -> GraphReader:
    """Process-wide reader, built on first use.

    Lazy for the reason the query embedder is: `create_app()` runs in
    `scripts/emit-openapi.py` with no configuration at all, and a schema dump must not
    need a graph.
    """
    global _reader
    if _reader is None:
        _reader = Neo4jGraphReader()
    return _reader


def reset_graph_reader() -> None:
    """Drop the cached reader. For tests and for a configuration change in development."""
    global _reader
    _reader = None


def _skip_reason(mode: RetrievalMode, *, paginated: bool) -> str | None:
    """Why the graph half will not run, decided before anything is spent."""
    if mode is RetrievalMode.VECTOR:
        return "disabled"
    if not graphrag_enabled():
        return "disabled"
    if not _graph_configured():
        return "not_configured"
    if paginated:
        return "paginated"
    return None


async def retrieve(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    query: str,
    query_vector: Sequence[float],
    k: int,
    after: tuple[float, UUID] | None = None,
    within: RetrievalWindow | None = None,
    mode: RetrievalMode = RetrievalMode.AUTO,
    reader: GraphReader | None = None,
    graph_limit: int = DEFAULT_GRAPH_LIMIT,
) -> HybridOutcome:
    """Retrieve evidence for one question, using the graph when it is available.

    The vector search is unconditional and unchanged — same function, same arguments, same
    ACL predicate, same escalation ladder. Everything after it is optional.
    """
    page = await search_chunks(
        session,
        user_id=user_id,
        query_vector=query_vector,
        k=k,
        after=after,
        within=within,
    )
    vector_evidence = tuple(page.items)

    reason = _skip_reason(mode, paginated=after is not None)
    if reason is not None:
        return _vector_only(page, vector_evidence, reason)

    started = time.monotonic()
    try:
        candidates = await asyncio.wait_for(
            (reader or get_graph_reader()).candidates(
                org_id=org_id,
                query=query,
                document_ids=[item.document_id for item in vector_evidence],
                limit=min(graph_limit, MAX_GRAPH_LIMIT),
            ),
            timeout=GRAPH_DEADLINE_S,
        )
    except TimeoutError:
        return _vector_only(page, vector_evidence, "timeout", elapsed_since=started)
    except Exception:
        # Every failure mode of an optional dependency, in one place: unreachable, refused
        # credentials, a driver that raised something this code has never seen. The
        # exception is deliberately not re-raised and deliberately not logged with its
        # text — a Neo4j connection error carries a host, a port and sometimes a
        # credential (§4.9), and the caller's answer is unaffected either way.
        logger.warning(
            "%s",
            {
                "event": "graph_retrieval_failed",
                "org_id": str(org_id),
                "reason": "error",
            },
        )
        return _vector_only(page, vector_evidence, "error", elapsed_since=started)

    # THE ACL GATE. Everything above this line is a suggestion from a store that knows
    # nothing about authorization; everything below it has been through `ACL_PREDICATE`
    # in Postgres, against this caller's own resolved principals.
    authorized = await fetch_evidence_many(
        session,
        user_id=user_id,
        chunk_ids=[candidate.chunk_id for candidate in candidates],
        query_vector=query_vector,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    vector_ranking = [item.chunk_id for item in vector_evidence]
    graph_ranking = [item.chunk_id for item in authorized]
    fused = reciprocal_rank_fusion([vector_ranking, graph_ranking])

    by_id: dict[UUID, Evidence] = {item.chunk_id: item for item in authorized}
    # The vector object wins on a collision: both carry the same measured similarity, and
    # preferring the one the search produced keeps a chunk found by both paths identical
    # to what a vector-only request would have returned for it.
    by_id.update({item.chunk_id: item for item in vector_evidence})

    evidence = tuple(by_id[chunk_id] for chunk_id, _ in fused if chunk_id in by_id)[:k]
    added = len({item.chunk_id for item in authorized} - set(vector_ranking))

    report = RetrievalReport(
        path=RetrievalPath.HYBRID,
        graph_candidates=len(candidates),
        graph_authorized=len(authorized),
        graph_dropped=len(candidates) - len(authorized),
        graph_added=added,
        graph_elapsed_ms=elapsed_ms,
        fallback_reason=None,
    )
    logger.info(
        "%s",
        {
            "event": "graph_retrieval_used",
            "org_id": str(org_id),
            "candidates": report.graph_candidates,
            "authorized": report.graph_authorized,
            "dropped": report.graph_dropped,
            "added": report.graph_added,
            "elapsed_ms": report.graph_elapsed_ms,
            "returned": len(evidence),
        },
    )
    return HybridOutcome(evidence=evidence, page=page, report=report)


def _vector_only(
    page: SearchPage,
    evidence: tuple[Evidence, ...],
    reason: str,
    *,
    elapsed_since: float | None = None,
) -> HybridOutcome:
    """The floor: exactly what retrieval returned before GraphRAG existed, plus a reason.

    A timeout is logged here and an exception is logged by the caller, which has the
    detail. The other three reasons — `disabled`, `not_configured`, `paginated` — are
    configuration rather than incident and are carried in the response instead: a
    deployment that never had a graph must not write a warning on every question.
    """
    if reason == "timeout":
        logger.warning("%s", {"event": "graph_retrieval_fallback", "reason": reason})
    return HybridOutcome(
        evidence=evidence,
        page=page,
        report=RetrievalReport(
            path=RetrievalPath.VECTOR,
            graph_candidates=0,
            graph_authorized=0,
            graph_dropped=0,
            graph_added=0,
            graph_elapsed_ms=(
                0 if elapsed_since is None else int((time.monotonic() - elapsed_since) * 1000)
            ),
            fallback_reason=reason,
        ),
    )
