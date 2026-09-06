"""The KT console over HTTP: the copilot, and what a recipient keeps between visits.

  kt:open   POST /v1/kt/{code}/ask
            GET  /v1/kt/{code}/conversations · POST …/conversations/search
            GET  /v1/kt/{code}/conversations/{id} · POST …/{id}/archive
            GET/POST /v1/kt/{code}/bookmarks · DELETE …/bookmarks/{id}
            GET  /v1/kt/{code}/progress · PUT/DELETE …/progress/{item_key}
            GET  /v1/kt/{code}/workspace

Every route is gated on `kt:open` — the permission every role holds — and then on the
package's own binding through `_open_for`, which each service function runs first. Roles
gate the feature; the package gates the person; the ACL predicate gates the data. No route
here takes an org id, a user id, principals, or a model name from the body.

**Why a KT ask route at all**, given `routers/kt.py` records the decision to have one
search path: that decision was about one *ACL predicate*, not one URL. This route calls
the same `search_chunks` — the window it passes is ANDed inside the predicate and cannot
widen it — and the same `synthesise_answer`, and spends the same search budget. What it
adds is what `/v1/ask` cannot carry without becoming KT-shaped: the package window, the
conversation, and the trail. ADR 0016 records the reasoning.

Question text travels in POST bodies only. Conversation search is a POST for that reason
(§4.9: user-authored text never reaches a URL, and a URL reaches every log there is).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from jutsu_core.errors import ServiceUnavailable
from jutsu_core.rbac import Permission
from jutsu_retrieval import DEFAULT_K
from pydantic import BaseModel, Field

from jutsu_api.answers import answers_configured
from jutsu_api.auth_service import scoped_acl_principals
from jutsu_api.deps import CurrentPrincipal, Db
from jutsu_api.kt_workspace import (
    add_bookmark,
    archive_conversation,
    ask_copilot,
    clear_progress,
    list_bookmarks,
    list_conversations,
    list_progress,
    read_conversation,
    read_workspace,
    remove_bookmark,
    set_progress,
)
from jutsu_api.rate_limit import spend_search_budget
from jutsu_api.retrieval import MAX_QUERY_CHARS
from jutsu_api.routers.search import (
    MAX_K,
    AnswerTransportDep,
    QueryEmbedderDep,
    SearchResultView,
)
from jutsu_api.security import GuardedAPIRoute, requires

router = APIRouter(prefix="/v1", tags=["kt"], route_class=GuardedAPIRoute)


# ------------------------------------------------------------------------- shapes


class ConversationOut(BaseModel):
    id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int


class ConversationPageOut(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None


class ConversationSearchPayload(BaseModel):
    """A POST, because `q` is the recipient's own words."""

    model_config = {"extra": "forbid"}

    q: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=50)


class StoredCitationOut(BaseModel):
    marker: int
    chunk_id: UUID
    document_id: UUID
    document_title: str
    source_system: str
    #: Re-decided on every read against the caller's ACL. False renders as "no longer
    #: available", never as a link.
    available: bool


class MessageOut(BaseModel):
    id: UUID
    role: str
    content: str
    citations: list[StoredCitationOut]
    insufficient_evidence: bool
    attempts: int
    created_at: datetime


class ConversationDetailOut(BaseModel):
    id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]


class CopilotAskPayload(BaseModel):
    """What the copilot may be asked. A question, optionally which conversation it
    continues, and how many passages to read. Nothing that names a tenant, a person, a
    filter, or a model."""

    model_config = {"extra": "forbid"}

    question: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    conversation_id: UUID | None = None
    k: int = Field(default=DEFAULT_K, ge=1, le=MAX_K)


class CopilotTurnOut(BaseModel):
    conversation_id: UUID
    question_message_id: UUID
    answer_message_id: UUID
    #: None exactly when `insufficient_evidence` is true.
    answer: str | None
    insufficient_evidence: bool
    citations: list[StoredCitationOut]
    #: The passages the answer stood on; every citation's marker indexes into this list.
    sources: list[SearchResultView]
    attempts: int
    query_tokens: int


class BookmarkPayload(BaseModel):
    model_config = {"extra": "forbid"}

    kind: str = Field(min_length=1, max_length=16)
    ref_id: UUID | None = None
    note: str | None = Field(default=None, max_length=2000)


class BookmarkOut(BaseModel):
    id: UUID
    kind: str
    ref_id: UUID | None
    note: str | None
    label: str
    available: bool
    tab: str | None
    created_at: datetime
    updated_at: datetime


class BookmarksOut(BaseModel):
    items: list[BookmarkOut]


class ProgressPayload(BaseModel):
    model_config = {"extra": "forbid"}

    state: str = Field(min_length=1, max_length=16)


class ProgressOut(BaseModel):
    item_key: str
    state: str
    updated_at: datetime


class ProgressListOut(BaseModel):
    items: list[ProgressOut]


class CoverageCategoryOut(BaseModel):
    category: str
    claim_type: str
    claims_visible: int


class CoverageOut(BaseModel):
    categories: list[CoverageCategoryOut]
    documents_visible: int
    documents_extracted: int
    chunks_covered: int
    chunks_total: int
    #: `chunks_covered / chunks_total` over the recipient's readable documents, or None
    #: when there is nothing to divide. The formula is the docstring of
    #: `kt_workspace.Coverage`; `reason` says which case this is.
    extraction_ratio: float | None
    reliable: bool
    reason: str


class LearningItemOut(BaseModel):
    key: str
    kind: str
    label: str
    why: str
    tab: str
    ref_id: UUID | None
    state: str | None


class LearningStageOut(BaseModel):
    day: int
    title: str
    items: list[LearningItemOut]


class RecommendationOut(BaseModel):
    key: str
    kind: str
    label: str
    why: str
    tab: str
    ref_id: UUID | None


class GapOut(BaseModel):
    key: str
    label: str
    why: str
    source: str
    tab: str | None
    ref_id: UUID | None


class ResumeOut(BaseModel):
    last_conversation: ConversationOut | None
    last_activity_at: datetime | None
    bookmarks: int
    unclear: int
    path_done: int
    path_total: int


class WorkspaceOut(BaseModel):
    coverage: CoverageOut
    learning_path: list[LearningStageOut]
    recommendations: list[RecommendationOut]
    gaps: list[GapOut]
    resume: ResumeOut


def _conversation(view: object) -> ConversationOut:
    return ConversationOut(**asdict(view))  # type: ignore[call-overload]


def _source(item: object) -> SearchResultView:
    return SearchResultView(
        chunk_id=str(item.chunk_id),  # type: ignore[attr-defined]
        document_id=str(item.document_id),  # type: ignore[attr-defined]
        document_title=item.document_title,  # type: ignore[attr-defined]
        source_system=item.source_system,  # type: ignore[attr-defined]
        text=item.text,  # type: ignore[attr-defined]
        char_start=item.char_start,  # type: ignore[attr-defined]
        char_end=item.char_end,  # type: ignore[attr-defined]
        score=item.score,  # type: ignore[attr-defined]
        occurred_at=item.occurred_at,  # type: ignore[attr-defined]
    )


# ------------------------------------------------------------------------ copilot


@router.post("/kt/{kt_code}/ask")
@requires(Permission.KT_OPEN)
async def ask(
    kt_code: str,
    payload: CopilotAskPayload,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
    embedder: QueryEmbedderDep,
    transport: AnswerTransportDep,
) -> CopilotTurnOut:
    """One turn of the KT copilot: a question answered from evidence inside the package
    window, with the conversation so far as context, and both turns kept.

    The ordering is the cost control, the same as `/v1/ask`: the free configuration gate
    first, then the budget on its own committed session, then the paid embedding, then
    retrieval and synthesis. A refused caller costs nothing and learns nothing.
    """
    if not answers_configured():
        raise ServiceUnavailable(
            "The KT copilot is not configured for this deployment yet. The knowledge "
            "tabs still work; answering in prose needs an answer model."
        )
    await spend_search_budget(org_id=principal.org_id, user_id=principal.user_id)

    turn = await ask_copilot(
        session,
        transport,
        embedder,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        question=payload.question,
        conversation_id=payload.conversation_id,
        k=payload.k,
        correlation_id=request.state.request_id,
    )
    return CopilotTurnOut(
        conversation_id=turn.conversation_id,
        question_message_id=turn.question_message_id,
        answer_message_id=turn.answer_message_id,
        answer=turn.answer,
        insufficient_evidence=turn.insufficient_evidence,
        citations=[StoredCitationOut(**asdict(c)) for c in turn.citations],
        sources=[_source(s) for s in turn.sources],
        attempts=turn.attempts,
        query_tokens=turn.query_tokens,
    )


# ------------------------------------------------------------------ conversations


@router.get("/kt/{kt_code}/conversations")
@requires(Permission.KT_OPEN)
async def read_conversations(
    kt_code: str,
    principal: CurrentPrincipal,
    session: Db,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
) -> ConversationPageOut:
    page = await list_conversations(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        limit=limit,
        cursor=cursor,
    )
    return ConversationPageOut(
        items=[_conversation(c) for c in page.items], next_cursor=page.next_cursor
    )


@router.post("/kt/{kt_code}/conversations/search")
@requires(Permission.KT_OPEN)
async def search_conversations(
    kt_code: str,
    payload: ConversationSearchPayload,
    principal: CurrentPrincipal,
    session: Db,
) -> ConversationPageOut:
    """Find earlier conversations by what was said in them. POST, so the words stay
    out of the URL."""
    page = await list_conversations(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        limit=payload.limit,
        cursor=None,
        q=payload.q,
    )
    return ConversationPageOut(
        items=[_conversation(c) for c in page.items], next_cursor=page.next_cursor
    )


@router.get("/kt/{kt_code}/conversations/{conversation_id}")
@requires(Permission.KT_OPEN)
async def read_one_conversation(
    kt_code: str,
    conversation_id: UUID,
    principal: CurrentPrincipal,
    session: Db,
) -> ConversationDetailOut:
    """A conversation with its turns. Citations are re-checked against the caller's
    ACL as of now; a cited document they can no longer read renders unavailable."""
    principals, groups = await scoped_acl_principals(session, user_id=principal.user_id)
    view = await read_conversation(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        conversation_id=conversation_id,
        principals=principals,
        groups=groups,
    )
    return ConversationDetailOut(
        id=view.id,
        title=view.title,
        created_at=view.created_at,
        updated_at=view.updated_at,
        messages=[
            MessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                citations=[StoredCitationOut(**asdict(c)) for c in m.citations],
                insufficient_evidence=m.insufficient_evidence,
                attempts=m.attempts,
                created_at=m.created_at,
            )
            for m in view.messages
        ],
    )


@router.post("/kt/{kt_code}/conversations/{conversation_id}/archive", status_code=204)
@requires(Permission.KT_OPEN)
async def archive_one_conversation(
    kt_code: str,
    conversation_id: UUID,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
) -> Response:
    await archive_conversation(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        conversation_id=conversation_id,
        correlation_id=request.state.request_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------- bookmarks


@router.get("/kt/{kt_code}/bookmarks")
@requires(Permission.KT_OPEN)
async def read_bookmarks(kt_code: str, principal: CurrentPrincipal, session: Db) -> BookmarksOut:
    principals, groups = await scoped_acl_principals(session, user_id=principal.user_id)
    items = await list_bookmarks(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        principals=principals,
        groups=groups,
    )
    return BookmarksOut(items=[BookmarkOut(**asdict(b)) for b in items])


@router.post("/kt/{kt_code}/bookmarks", status_code=status.HTTP_201_CREATED)
@requires(Permission.KT_OPEN)
async def create_bookmark(
    kt_code: str,
    payload: BookmarkPayload,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
) -> BookmarkOut:
    """Save a claim, document, message or question. A claim or document must be visible
    to the caller under the package's gates before it is saved — the refusal is the same
    404 as for an id that never existed."""
    principals, groups = await scoped_acl_principals(session, user_id=principal.user_id)
    view = await add_bookmark(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        kind=payload.kind,
        ref_id=payload.ref_id,
        note=payload.note,
        principals=principals,
        groups=groups,
        correlation_id=request.state.request_id,
    )
    return BookmarkOut(**asdict(view))


@router.delete("/kt/{kt_code}/bookmarks/{bookmark_id}", status_code=204)
@requires(Permission.KT_OPEN)
async def delete_bookmark(
    kt_code: str, bookmark_id: UUID, principal: CurrentPrincipal, session: Db
) -> Response:
    await remove_bookmark(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        bookmark_id=bookmark_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------ progress


@router.get("/kt/{kt_code}/progress")
@requires(Permission.KT_OPEN)
async def read_progress(kt_code: str, principal: CurrentPrincipal, session: Db) -> ProgressListOut:
    items = await list_progress(
        session, org_id=principal.org_id, user_id=principal.user_id, kt_code=kt_code
    )
    return ProgressListOut(items=[ProgressOut(**asdict(i)) for i in items])


@router.put("/kt/{kt_code}/progress/{item_key}")
@requires(Permission.KT_OPEN)
async def write_progress(
    kt_code: str,
    item_key: str,
    payload: ProgressPayload,
    principal: CurrentPrincipal,
    session: Db,
) -> ProgressOut:
    item = await set_progress(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        item_key=item_key,
        state=payload.state,
    )
    return ProgressOut(**asdict(item))


@router.delete("/kt/{kt_code}/progress/{item_key}", status_code=204)
@requires(Permission.KT_OPEN)
async def delete_progress(
    kt_code: str, item_key: str, principal: CurrentPrincipal, session: Db
) -> Response:
    await clear_progress(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        item_key=item_key,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------- workspace


@router.get("/kt/{kt_code}/workspace")
@requires(Permission.KT_OPEN)
async def read_kt_workspace(kt_code: str, principal: CurrentPrincipal, session: Db) -> WorkspaceOut:
    """Coverage, the learning path, recommendations, gaps and the resume card in one
    round trip — all computed now, from the caller's visible evidence, none of it
    stored except their own progress markers."""
    principals, groups = await scoped_acl_principals(session, user_id=principal.user_id)
    workspace = await read_workspace(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        principals=principals,
        groups=groups,
    )
    return WorkspaceOut(
        coverage=CoverageOut(
            categories=[CoverageCategoryOut(**asdict(c)) for c in workspace.coverage.categories],
            documents_visible=workspace.coverage.documents_visible,
            documents_extracted=workspace.coverage.documents_extracted,
            chunks_covered=workspace.coverage.chunks_covered,
            chunks_total=workspace.coverage.chunks_total,
            extraction_ratio=workspace.coverage.extraction_ratio,
            reliable=workspace.coverage.reliable,
            reason=workspace.coverage.reason,
        ),
        learning_path=[
            LearningStageOut(
                day=stage.day,
                title=stage.title,
                items=[LearningItemOut(**asdict(i)) for i in stage.items],
            )
            for stage in workspace.learning_path
        ],
        recommendations=[RecommendationOut(**asdict(r)) for r in workspace.recommendations],
        gaps=[GapOut(**asdict(g)) for g in workspace.gaps],
        resume=ResumeOut(
            last_conversation=(
                _conversation(workspace.resume.last_conversation)
                if workspace.resume.last_conversation
                else None
            ),
            last_activity_at=workspace.resume.last_activity_at,
            bookmarks=workspace.resume.bookmarks,
            unclear=workspace.resume.unclear,
            path_done=workspace.resume.path_done,
            path_total=workspace.resume.path_total,
        ),
    )
