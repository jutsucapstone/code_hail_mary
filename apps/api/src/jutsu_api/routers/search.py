"""Search: ask the corpus a question, get authorized evidence back (§12).

```
POST /v1/search    retrieval:query
```

**POST, not GET, and the reason is not CSRF.** The query is user-authored text — a
question about a person, a deal, an incident — and a GET puts it in the request line,
which is the one part of a request that reaches access logs, proxy logs, browser history
and `Referer` headers by default. §4.9 forbids exactly that. POST costs a CSRF header
and keeps the question in a body nothing logs.

`retrieval:query` is held by every role, and that is §17 obeyed rather than relaxed:
roles gate features, ACLs gate data. Asking the corporate memory a question is the
feature every employee is hired to use. *Which* evidence comes back is decided inside
`search_chunks` by `document_acl`, per caller, per request.

**Nothing about the tenant or the principal set is a parameter.** The organisation comes
from the session GUC that `resolve_principal` set, and the principals are resolved inside
the query from the caller's user id. There is no field on this endpoint a browser can
edit to widen what it sees, and adding one — an `org_id`, a `principals`, a
`min_score` that skipped the filter — would be the regression ADR 0011 was written to
prevent.

An empty result is a 200 with an empty list, never a 404. A caller with no linked source
identity retrieves nothing, and that is the system working: they hold no `user` or
`group` grant. Distinguishing "nothing matched" from "nothing you may see" is precisely
the existence oracle §4.5 forbids.

**Both routes retrieve through `jutsu_api.graphrag.retrieve`**, which runs the same
`search_chunks` first and then, where the graph is enabled and reachable, fuses in
candidates the graph suggested — every one of them re-authorized in SQL before it is
returned. On a deployment without a graph, with the flag off, or with Neo4j unreachable,
that function is `search_chunks` plus a reason string, so these endpoints answer exactly
as they did before GraphRAG existed (ADR 0022).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from jutsu_core.errors import ServiceUnavailable
from jutsu_core.rbac import Permission
from jutsu_retrieval import DEFAULT_K
from pydantic import BaseModel, Field

from jutsu_api.answers import (
    AnswerTransport,
    Citation,
    answers_configured,
    synthesise_answer,
)
from jutsu_api.deps import CurrentPrincipal, Db
from jutsu_api.graphrag import RetrievalMode, RetrievalReport, retrieve
from jutsu_api.llm import FailoverTransport
from jutsu_api.rate_limit import spend_search_budget
from jutsu_api.retrieval import (
    MAX_QUERY_CHARS,
    QueryEmbedder,
    decode_cursor,
    encode_cursor,
    get_query_embedder,
)
from jutsu_api.security import GuardedAPIRoute, requires

#: Injected rather than called, so a test can supply a scripted transport and the suite
#: never needs a credential. The same seam `EmbeddingTransport` already draws in S6,
#: drawn once more at the HTTP boundary.
QueryEmbedderDep = Annotated[QueryEmbedder, Depends(get_query_embedder)]

router = APIRouter(prefix="/v1", tags=["search"], route_class=GuardedAPIRoute)

#: Upper bound on `k`. The default ladder tops out at `ef_search = 1000`, so asking for
#: more than this cannot be answered well however hard the index looks, and an unbounded
#: `k` is an unbounded response body on a paid endpoint.
MAX_K = 100


class SearchRequest(BaseModel):
    """What the caller may ask. Every field here is a *relevance* control.

    There is deliberately no filter over documents, sources, people or dates. Not because
    filtering is unwanted — §12 will want it — but because each one has to be pushed
    into the same SQL that carries `ACL_PREDICATE`, and a filter applied after the ACL
    is a filter that can be made to reveal counts. They arrive with the query that
    supports them, not before.
    """

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    k: int = Field(default=DEFAULT_K, ge=1, le=MAX_K)
    #: Opaque keyset token from a previous response's `next_cursor`.
    cursor: str | None = None
    #: Which retrieval path to use. **Defaults to `vector` here, and only here.**
    #:
    #: This endpoint paginates, and `next_cursor` is a keyset over the *vector* ordering.
    #: A fused ranking is not in that order, so a fused first page followed by a vector
    #: second page can skip a passage that fusion displaced. Rather than pretend
    #: otherwise, the paginated surface keeps exactly the contract it has always had, and
    #: a caller who is not paginating can ask for `hybrid` explicitly. `/v1/ask` — which
    #: returns one bounded set and never paginates — defaults to `auto` instead.
    #:
    #: A cursor plus `hybrid` is not an error: the graph half is skipped and the response
    #: says `paginated`, because refusing the request would be a worse answer than the
    #: correct one.
    retrieval_mode: RetrievalMode = RetrievalMode.VECTOR


class SearchResultView(BaseModel):
    """One retrieved chunk (§12 `Citation`).

    `char_start` and `char_end` index the **original** document body; `text` is the
    masked body. Highlighting `text` with those offsets mis-highlights the span, because
    masking changes lengths — the trap CLAUDE.md records. To render a highlight, call
    `GET /v1/evidence/{chunk_id}` and use them against the source document there.
    """

    chunk_id: str
    document_id: str
    document_title: str
    source_system: str
    #: Masked text (§9.1), never the original body. ADR 0005 is explicit that this still
    #: contains names — masking covers addresses, phones, cards, IBANs and SSNs, not
    #: people — so it is not public, it is what an authorized caller may read.
    text: str
    char_start: int
    char_end: int
    #: Cosine similarity in `[0, 1]`. Reported for ranking and for eval. Nothing
    #: thresholds on it: a similarity floor is a relevance decision, not an
    #: authorization one, and the two must not be confused in the same number.
    score: float
    occurred_at: datetime


class SearchStatsView(BaseModel):
    """What the search cost. Opaque counts and timings, safe to show and to log.

    `exhausted` is the one worth rendering: true means the escalation ladder stopped
    with fewer than `k` rows, which usually means the caller is not authorized to see
    `k` documents. That is the system working, and a UI that reads it as an error
    teaches people the search is broken when it is being correct.
    """

    attempts: int
    ef_search: int
    returned: int
    elapsed_ms: int
    exhausted: bool


class RetrievalView(BaseModel):
    """Which retrieval path answered, and what the graph half contributed.

    Reported rather than requested: `mode` is what *happened*, which is not always what
    was asked for — a deployment with no graph, a graph that timed out and a caller who
    asked for vector all answer `vector`, and `fallback_reason` is the only thing that
    tells them apart.

    `graph_dropped` counts graph candidates that did not survive the ACL fetch. It is a
    count of this caller's own denials — no document, no title, no identifier — and it is
    here because an authorization filter with no observable effect is one nobody would
    notice failing open.
    """

    #: `vector` or `hybrid`.
    mode: str
    graph_candidates: int
    graph_authorized: int
    graph_dropped: int
    #: Passages the graph contributed that the vector search had not already found.
    graph_added: int
    graph_elapsed_ms: int
    #: `disabled`, `not_configured`, `paginated`, `timeout`, `error` — or null when the
    #: graph half ran.
    fallback_reason: str | None = None


def _retrieval_view(report: RetrievalReport) -> RetrievalView:
    return RetrievalView(
        mode=report.path.value,
        graph_candidates=report.graph_candidates,
        graph_authorized=report.graph_authorized,
        graph_dropped=report.graph_dropped,
        graph_added=report.graph_added,
        graph_elapsed_ms=report.graph_elapsed_ms,
        fallback_reason=report.fallback_reason,
    )


class SearchResponse(BaseModel):
    items: list[SearchResultView]
    stats: SearchStatsView
    #: Present when there may be more. Feed it back as `cursor`; it is a keyset
    #: position, not an offset, so a document becoming visible between pages cannot
    #: shift the window.
    next_cursor: str | None = None
    #: The provider's own token count for embedding this query, never an estimate.
    #: Surfaced because §20 asks for cost to be visible on the paid path rather than
    #: reconstructed from a bill.
    query_tokens: int
    #: Which path answered. Always present; `mode` is `vector` on every deployment that
    #: has not enabled the graph, which is the default.
    retrieval: RetrievalView


@router.post("/search")
@requires(Permission.RETRIEVAL_QUERY)
async def search(
    payload: SearchRequest,
    principal: CurrentPrincipal,
    session: Db,
    embedder: QueryEmbedderDep,
) -> SearchResponse:
    """Top-`k` chunks this caller is authorized to read, nearest first.

    The order of these three steps is the cost control, and it is not arbitrary.

    The cursor is validated first, so a malformed pagination token is a free 422 rather
    than one that has already paid the provider. The budget is spent second — **before**
    the embedding call, so a caller over their limit costs nothing at all. The paid call
    happens last, once the request is known to be well formed and permitted.

    The spend commits on its own session, so a failure after this point still consumes
    the quota. That is the policy: an attempt is what costs money, and counting only
    successes would leave a caller whose requests all fail with no limit at all.
    """
    after = decode_cursor(payload.cursor) if payload.cursor is not None else None

    await spend_search_budget(org_id=principal.org_id, user_id=principal.user_id)

    vector, query_tokens = await embedder.embed(payload.query)

    outcome = await retrieve(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        query=payload.query,
        query_vector=vector,
        k=payload.k,
        after=after,
        mode=payload.retrieval_mode,
    )
    page = outcome.page

    return SearchResponse(
        items=[
            SearchResultView(
                chunk_id=str(item.chunk_id),
                document_id=str(item.document_id),
                document_title=item.document_title,
                source_system=item.source_system,
                text=item.text,
                char_start=item.char_start,
                char_end=item.char_end,
                score=item.score,
                occurred_at=item.occurred_at,
            )
            for item in outcome.evidence
        ],
        # The vector half's own numbers, unchanged. They describe the index scan that
        # produced `page`, not the fusion — `exhausted` in particular is a statement
        # about the ACL and the ladder, and reporting a fused count there would make it
        # mean something different on a deployment with a graph.
        stats=SearchStatsView(
            attempts=page.stats.attempts,
            ef_search=page.stats.ef_search,
            returned=page.stats.returned,
            elapsed_ms=page.stats.elapsed_ms,
            exhausted=page.stats.exhausted,
        ),
        next_cursor=(
            encode_cursor(page.next_cursor[0], page.next_cursor[1])
            if page.next_cursor is not None
            else None
        ),
        query_tokens=query_tokens,
        retrieval=_retrieval_view(outcome.report),
    )


class AskRequest(BaseModel):
    model_config = {"extra": "forbid"}

    question: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    k: int = Field(default=DEFAULT_K, ge=1, le=MAX_K)
    #: `auto` here, unlike `/v1/search`: this endpoint returns one bounded set and never
    #: paginates, so the objection that keeps fusion off the paginated surface does not
    #: apply. A deployment with no graph, or with the flag off, answers exactly as it did
    #: before — `auto` means "use it if it is there", not "require it".
    retrieval_mode: RetrievalMode = RetrievalMode.AUTO


class CitationView(BaseModel):
    marker: int
    chunk_id: str
    document_id: str
    document_title: str
    source_system: str


class AskResponse(BaseModel):
    """A grounded answer, or an honest refusal — never a fluent guess.

    `answer` is None exactly when `insufficient_evidence` is true. `sources` carries
    the retrieved passages so the UI can render what the answer was grounded ON, and
    every citation's `marker` indexes into it (1-based).
    """

    answer: str | None
    insufficient_evidence: bool
    citations: list[CitationView]
    sources: list[SearchResultView]
    attempts: int
    query_tokens: int
    #: Which path assembled `sources`. A graph-contributed passage is cited exactly like
    #: any other — by chunk, document and source system — because the citation is the
    #: evidence, and the graph is an index over evidence rather than a source of it.
    retrieval: RetrievalView


def get_answer_transport() -> AnswerTransport:
    """The model call, through the provider chain (ADR 0023).

    Claude first, then whichever fallbacks this deployment has configured. A chain with
    one provider in it is the behaviour that shipped before the chain existed, which is
    what a deployment holding only `ANTHROPIC_API_KEY` still gets.

    Built per request rather than cached: the providers hold no connection pool between
    calls, so there is no shared mutable state for concurrent requests to contend over,
    and a rotated key takes effect without a restart. Tests override this dependency
    exactly as they always have.
    """
    return FailoverTransport()


AnswerTransportDep = Annotated[AnswerTransport, Depends(get_answer_transport)]


@router.post("/ask")
@requires(Permission.RETRIEVAL_QUERY)
async def ask(
    payload: AskRequest,
    principal: CurrentPrincipal,
    session: Db,
    embedder: QueryEmbedderDep,
    transport: AnswerTransportDep,
) -> AskResponse:
    """Answer a question from retrieved evidence (non-negotiable 3), or refuse.

    Ordering is the cost control, exactly as on /v1/search — with one extra gate at the
    very front: an unconfigured answer service refuses before a single token is spent
    on budget, embedding or retrieval, because "search works, answers are not set up"
    is a fact the caller deserves for free.

    The same permission as search, deliberately: composing retrieved evidence into a
    cited paragraph grants access to nothing the caller could not already read one
    passage at a time.
    """
    if not answers_configured():
        raise ServiceUnavailable(
            "Answers are not configured for this deployment yet. Retrieval still works — "
            "an administrator must add the answer provider's credentials."
        )

    await spend_search_budget(org_id=principal.org_id, user_id=principal.user_id)

    vector, query_tokens = await embedder.embed(payload.question)

    retrieved = await retrieve(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        query=payload.question,
        query_vector=vector,
        k=payload.k,
        after=None,
        mode=payload.retrieval_mode,
    )

    outcome = await synthesise_answer(
        transport, question=payload.question, evidence=list(retrieved.evidence)
    )

    return AskResponse(
        answer=outcome.answer,
        insufficient_evidence=outcome.insufficient_evidence,
        citations=[CitationView(**vars_citation(c)) for c in outcome.citations],
        sources=[
            SearchResultView(
                chunk_id=str(item.chunk_id),
                document_id=str(item.document_id),
                document_title=item.document_title,
                source_system=item.source_system,
                text=item.text,
                char_start=item.char_start,
                char_end=item.char_end,
                score=item.score,
                occurred_at=item.occurred_at,
            )
            for item in retrieved.evidence
        ],
        attempts=outcome.attempts,
        query_tokens=query_tokens,
        retrieval=_retrieval_view(retrieved.report),
    )


def vars_citation(citation: Citation) -> dict[str, object]:
    return asdict(citation)
