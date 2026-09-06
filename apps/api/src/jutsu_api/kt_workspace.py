"""The KT console's workspace: what a recipient asks, keeps, and is shown next.

`jutsu_api.kt` decides *whether* a recipient may be in a package and serves the knowledge
inside it. This module is everything a recipient does with that knowledge across visits:

* **the copilot** — a question, answered from evidence inside the package window, with
  the conversation so far as context and every turn kept;
* **conversations, bookmarks, progress** — the three things migration 0019 lets a
  recipient own;
* **the workspace** — coverage, a learning path, recommendations, "still unclear" and a
  resume card, all *computed on demand from the recipient's own visible evidence*.

Four rules hold everywhere here, and each one is the reason a shortcut was not taken.

**Every function opens the package first.** `_open_for` is the single authorization path
— binding, expiry, revocation, denied-open audit — and a revoked package must close the
history, the bookmarks and the copilot on the next request, not after a cache window.
Nothing in this module reads `kt_packages` any other way.

**Retrieval narrows, it never widens.** The copilot calls the same `search_chunks` as
`/v1/ask`, with the package's period passed as a `RetrievalWindow` that is ANDed inside
the ACL predicate. There is one retrieval function and now two callers; there is still
one place an ACL bug could live. Claims and documents shown here run under the same
`ACL_PREDICATE` the knowledge tabs use, so a count can never exceed what a tab would list.

**History is context, never evidence.** Prior turns reach the model as a labelled
preamble (`answers.Turn`); the citation gate resolves markers against retrieved passages
alone, so an earlier answer cannot become a source for a later one.

**Nothing here is generated to fill a gap.** The learning path, recommendations and
gaps are ordered lists of *real* claims and documents with a stated `why`; coverage is
counts plus one ratio whose formula is written down (`Coverage`), and it says "cannot be
calculated" when the inputs are missing rather than printing a plausible percentage.
People are never scored or ranked (non-negotiables 16-18): the People stage lists the
most recent mentions and says that is the order.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from jutsu_core.errors import NotFound, ValidationFailed
from jutsu_retrieval.search import ACL_PREDICATE, Evidence, RetrievalWindow, search_chunks
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.answers import AnswerTransport, Turn, synthesise_answer
from jutsu_api.kt import (
    _CLAIM_SCOPE,
    _LATEST_RUN_JOIN,
    KtInsight,
    _audit,
    _open_for,
    _touch_activity,
    kt_documents,
    kt_insight_summary,
    kt_insights,
)
from jutsu_api.retrieval import QueryEmbedder

__all__ = [
    "BOOKMARK_KINDS",
    "HISTORY_CHARS",
    "HISTORY_TURNS",
    "PROGRESS_STATES",
    "BookmarkView",
    "ConversationPage",
    "ConversationSummary",
    "ConversationView",
    "CopilotTurn",
    "Coverage",
    "CoverageCategory",
    "Gap",
    "LearningItem",
    "LearningStage",
    "MessageView",
    "ProgressItem",
    "Recommendation",
    "ResumeCard",
    "StoredCitation",
    "Workspace",
    "add_bookmark",
    "archive_conversation",
    "ask_copilot",
    "clear_progress",
    "list_bookmarks",
    "list_conversations",
    "list_progress",
    "read_conversation",
    "read_workspace",
    "remove_bookmark",
    "set_progress",
]

#: How much conversation the model sees. Three exchanges is enough to resolve "and who
#: owned that?"; more is prompt spend on turns the question no longer refers to.
HISTORY_TURNS: Final = 6
#: A turn is truncated, not summarised — a summary would be generated text in the prompt.
HISTORY_CHARS: Final = 1_000

PROGRESS_STATES: Final = ("seen", "done", "unclear")
BOOKMARK_KINDS: Final = ("claim", "document", "message", "question")

#: `claim:{uuid}` | `document:{uuid}` | `step:{key}`. The learning path, the knowledge tabs
#: and the progress table agree on this spelling, and it is validated before it is stored.
_ITEM_KEY: Final = re.compile(r"^(claim|document|step):[A-Za-z0-9_.:-]{1,120}$")

_CONVERSATION_NOT_FOUND: Final = "That conversation was not found."
_BOOKMARK_NOT_FOUND: Final = "That bookmark was not found."
#: Deliberately the same sentence for "no such thing" and "not yours to see" — a bookmark
#: of a claim the caller cannot read must not confirm the claim exists.
_REF_NOT_FOUND: Final = "That item is not available to you."

#: Which tab a kind of item lives on, so the client can link without knowing the mapping.
_TAB_FOR_TYPE: Final[dict[str, str]] = {
    "decision": "decisions",
    "person": "people",
    "project": "projects",
    "meeting": "meetings",
    "responsibility": "responsibilities",
}


# ------------------------------------------------------------------------- views


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int


@dataclass(frozen=True, slots=True)
class ConversationPage:
    items: list[ConversationSummary]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class StoredCitation:
    """A citation as it was kept: references and display data, never evidence text.

    `available` is re-decided on every read by running the cited documents through the
    ACL predicate. A citation whose document the caller can no longer read renders as
    "no longer available" rather than as a link that would 404 — or, worse, resolve.
    """

    marker: int
    chunk_id: UUID
    document_id: UUID
    document_title: str
    source_system: str
    available: bool


@dataclass(frozen=True, slots=True)
class MessageView:
    id: UUID
    role: str
    content: str
    citations: list[StoredCitation]
    insufficient_evidence: bool
    attempts: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationView:
    id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageView]


@dataclass(frozen=True, slots=True)
class CopilotTurn:
    conversation_id: UUID
    question_message_id: UUID
    answer_message_id: UUID
    answer: str | None
    insufficient_evidence: bool
    citations: list[StoredCitation]
    #: The retrieved passages the answer stood on, for the client to render what was
    #: read. Every citation's marker indexes into this list (1-based).
    sources: list[Evidence]
    attempts: int
    query_tokens: int


@dataclass(frozen=True, slots=True)
class BookmarkView:
    id: UUID
    kind: str
    ref_id: UUID | None
    note: str | None
    #: What the bookmark points at, as the recipient would recognise it. For a claim or
    #: document this is re-resolved under ACL on every list; `available` says whether it
    #: still resolves.
    label: str
    available: bool
    tab: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProgressItem:
    item_key: str
    state: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CoverageCategory:
    category: str
    claim_type: str
    #: Claims of this type inside the window that THIS recipient may read — the same
    #: count the tab would show, computed under the same predicate.
    claims_visible: int


@dataclass(frozen=True, slots=True)
class Coverage:
    """What the package holds for this recipient, and how much of it extraction has seen.

    The one ratio here has one formula: `chunks_covered / chunks_total`, summed over the
    latest finished extraction run of every document in the window the recipient may
    read. Extraction reads a prefix of long documents and records how far it got
    (`extraction_runs.stats_json`), so this is the fraction of readable text that could
    have produced a claim at all. It is `None` — and `reliable` is False — when there is
    nothing to divide: no readable documents, or none of them extracted. That state is
    reported in `reason`, never rounded to a number.

    Nothing here is a percentage of "what there is to know". That quantity does not exist
    in any table, and a figure standing in for it would be invented (rule 8).
    """

    categories: list[CoverageCategory]
    documents_visible: int
    documents_extracted: int
    chunks_covered: int
    chunks_total: int
    extraction_ratio: float | None
    reliable: bool
    reason: str


@dataclass(frozen=True, slots=True)
class LearningItem:
    key: str
    kind: str
    label: str
    why: str
    tab: str
    ref_id: UUID | None
    state: str | None


@dataclass(frozen=True, slots=True)
class LearningStage:
    day: int
    title: str
    items: list[LearningItem]


@dataclass(frozen=True, slots=True)
class Recommendation:
    key: str
    kind: str
    label: str
    why: str
    tab: str
    ref_id: UUID | None


@dataclass(frozen=True, slots=True)
class Gap:
    key: str
    label: str
    why: str
    #: "you" for something the recipient marked unclear; "evidence" for something the
    #: data itself shows is missing.
    source: str
    tab: str | None
    ref_id: UUID | None


@dataclass(frozen=True, slots=True)
class ResumeCard:
    last_conversation: ConversationSummary | None
    last_activity_at: datetime | None
    bookmarks: int
    unclear: int
    path_done: int
    path_total: int


@dataclass(frozen=True, slots=True)
class Workspace:
    coverage: Coverage
    learning_path: list[LearningStage]
    recommendations: list[Recommendation]
    gaps: list[Gap]
    resume: ResumeCard


# ----------------------------------------------------------------------- helpers


def _window(row: object) -> RetrievalWindow | None:
    start = getattr(row, "period_start", None)
    end = getattr(row, "period_end", None)
    if start is None and end is None:
        return None
    return RetrievalWindow(created_from=start, created_to=end)


def _period_filters(row: object, params: dict[str, object]) -> list[str]:
    """The package window as SQL on `d`, binding what it uses — the kt.py shape."""
    filters: list[str] = []
    start = getattr(row, "period_start", None)
    end = getattr(row, "period_end", None)
    if start is not None:
        params["period_start"] = start
        filters.append("d.created_at >= :period_start")
    if end is not None:
        params["period_end"] = end
        filters.append("d.created_at <= :period_end")
    return filters


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        ts, last_id = cursor.split("|", 1)
        return datetime.fromisoformat(ts), UUID(last_id)
    except (ValueError, AttributeError) as exc:
        raise NotFound("That page does not exist.") from exc


def _label_for_claim(insight: KtInsight) -> str:
    if insight.name and insight.summary:
        return f"{insight.name} — {insight.summary}"
    return (insight.name or insight.summary or insight.quote)[:160]


def _why_for_claim(insight: KtInsight) -> str:
    when = insight.date or insight.occurred_at.strftime("%Y-%m-%d")
    return f"{insight.source_system} · {insight.document_title} · {when}"


async def _visible_documents(
    session: AsyncSession,
    *,
    document_ids: list[UUID],
    principals: frozenset[str],
    groups: frozenset[str],
) -> set[UUID]:
    """Which of these documents the caller may read right now — the ACL predicate over a
    stored reference, which is how a kept citation or bookmark stays honest."""
    if not document_ids:
        return set()
    rows = (
        await session.execute(
            text(
                "SELECT d.id FROM documents d "  # noqa: S608
                f"WHERE d.id = ANY(:ids) AND d.superseded_by IS NULL AND {ACL_PREDICATE}"
            ),
            {
                "ids": [str(d) for d in document_ids],
                "principals": list(principals),
                "groups": list(groups),
            },
        )
    ).all()
    return {UUID(str(r.id)) for r in rows}


async def _owned_conversation(
    session: AsyncSession, *, package_id: UUID, user_id: UUID, conversation_id: UUID
) -> object:
    """A conversation belongs to exactly one recipient of exactly one package.

    Scoped by both columns, under RLS. Another person's conversation in the same
    package, or the same person's conversation in another package, is the same 404 as
    one that never existed.
    """
    row = (
        await session.execute(
            text(
                "SELECT id, title, created_at, updated_at FROM kt_conversations "
                "WHERE id = :id AND kt_package_id = :pkg AND user_id = :user "
                "AND archived_at IS NULL"
            ),
            {"id": conversation_id, "pkg": package_id, "user": user_id},
        )
    ).first()
    if row is None:
        raise NotFound(_CONVERSATION_NOT_FOUND)
    return row


# ------------------------------------------------------------------ conversations


async def list_conversations(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    limit: int,
    cursor: str | None,
    q: str | None = None,
) -> ConversationPage:
    """The recipient's own conversations in this package, most recent first.

    `q` is a plain substring over the recipient's own turns. It arrives in a POST body
    (`routers/kt_console.py`), never a query string — it is user-authored text.
    """
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    bounded = max(1, min(limit, 50))
    filters = ["c.kt_package_id = :pkg", "c.user_id = :user", "c.archived_at IS NULL"]
    params: dict[str, object] = {
        "pkg": row.id,  # type: ignore[attr-defined]
        "user": user_id,
        "limit": bounded + 1,
    }
    if cursor:
        params["cursor_ts"], params["cursor_id"] = _decode_cursor(cursor)
        filters.append("(c.updated_at, c.id) < (:cursor_ts, :cursor_id)")
    if q:
        params["needle"] = f"%{q.strip()}%"
        filters.append(
            "EXISTS (SELECT 1 FROM kt_messages m WHERE m.conversation_id = c.id "
            "AND m.content ILIKE :needle)"
        )

    rows = (
        await session.execute(
            text(
                "SELECT c.id, c.title, c.created_at, c.updated_at, "  # noqa: S608
                "(SELECT count(*) FROM kt_messages m WHERE m.conversation_id = c.id) "
                "AS message_count "
                f"FROM kt_conversations c WHERE {' AND '.join(filters)} "
                "ORDER BY c.updated_at DESC, c.id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    page = rows[:bounded]
    next_cursor = (
        f"{page[-1].updated_at.isoformat()}|{page[-1].id}" if len(rows) > bounded and page else None
    )
    return ConversationPage(
        items=[
            ConversationSummary(
                id=r.id,
                title=r.title,
                created_at=r.created_at,
                updated_at=r.updated_at,
                message_count=int(r.message_count),
            )
            for r in page
        ],
        next_cursor=next_cursor,
    )


def _citations_from_json(raw: object, visible: set[UUID]) -> list[StoredCitation]:
    items = raw if isinstance(raw, list) else []
    out: list[StoredCitation] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            document_id = UUID(str(item["document_id"]))
            out.append(
                StoredCitation(
                    marker=int(item["marker"]),
                    chunk_id=UUID(str(item["chunk_id"])),
                    document_id=document_id,
                    document_title=str(item.get("document_title", "")),
                    source_system=str(item.get("source_system", "")),
                    available=document_id in visible,
                )
            )
        except (KeyError, ValueError, TypeError):
            # A malformed stored citation is dropped, not rendered as a link to nowhere.
            continue
    return out


async def read_conversation(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    conversation_id: UUID,
    principals: frozenset[str],
    groups: frozenset[str],
    limit: int = 200,
) -> ConversationView:
    """One conversation with its turns, citations re-checked against today's ACL."""
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    conversation = await _owned_conversation(
        session,
        package_id=row.id,  # type: ignore[attr-defined]
        user_id=user_id,
        conversation_id=conversation_id,
    )
    await _touch_activity(session, package_id=row.id)  # type: ignore[attr-defined]

    messages = (
        await session.execute(
            text(
                "SELECT id, role, content, citations_json, insufficient_evidence, attempts, "
                "created_at FROM kt_messages WHERE conversation_id = :c "
                "ORDER BY created_at ASC, id ASC LIMIT :limit"
            ),
            {"c": conversation_id, "limit": max(1, min(limit, 500))},
        )
    ).all()

    cited: list[UUID] = []
    for m in messages:
        for item in m.citations_json if isinstance(m.citations_json, list) else []:
            if isinstance(item, dict) and "document_id" in item:
                try:
                    cited.append(UUID(str(item["document_id"])))
                except ValueError:
                    continue
    visible = await _visible_documents(
        session, document_ids=cited, principals=principals, groups=groups
    )

    return ConversationView(
        id=conversation.id,  # type: ignore[attr-defined]
        title=conversation.title,  # type: ignore[attr-defined]
        created_at=conversation.created_at,  # type: ignore[attr-defined]
        updated_at=conversation.updated_at,  # type: ignore[attr-defined]
        messages=[
            MessageView(
                id=m.id,
                role=m.role,
                content=m.content,
                citations=_citations_from_json(m.citations_json, visible),
                insufficient_evidence=bool(m.insufficient_evidence),
                attempts=int(m.attempts),
                created_at=m.created_at,
            )
            for m in messages
        ],
    )


async def archive_conversation(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    conversation_id: UUID,
    correlation_id: str | None = None,
) -> None:
    """Hide a conversation from the recipient's list. Archived, not deleted: the turns
    are the recipient's own record, and a soft close keeps the option of an erasure
    path that is designed rather than improvised."""
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    await _owned_conversation(
        session,
        package_id=row.id,  # type: ignore[attr-defined]
        user_id=user_id,
        conversation_id=conversation_id,
    )
    await session.execute(
        text("UPDATE kt_conversations SET archived_at = now() WHERE id = :id"),
        {"id": conversation_id},
    )
    await _audit(
        session,
        org_id=org_id,
        actor_id=user_id,
        action="kt.conversation_archived",
        resource_id=row.id,  # type: ignore[attr-defined]
        correlation_id=correlation_id,
        meta={"conversation_id": str(conversation_id)},
    )


# ------------------------------------------------------------------------ copilot


async def _history(session: AsyncSession, *, conversation_id: UUID) -> list[Turn]:
    rows = (
        await session.execute(
            text(
                "SELECT role, content FROM kt_messages WHERE conversation_id = :c "
                "ORDER BY created_at DESC, id DESC LIMIT :n"
            ),
            {"c": conversation_id, "n": HISTORY_TURNS},
        )
    ).all()
    # Oldest first for the model; each turn bounded so a long answer cannot crowd the
    # passages out of the prompt.
    return [Turn(role=r.role, content=r.content[:HISTORY_CHARS]) for r in reversed(rows)]


async def ask_copilot(
    session: AsyncSession,
    transport: AnswerTransport,
    embedder: QueryEmbedder,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    question: str,
    conversation_id: UUID | None,
    k: int,
    correlation_id: str | None = None,
) -> CopilotTurn:
    """One turn of the KT copilot.

    Order: open the package; find or start the conversation; retrieve inside the window
    under the recipient's own ACL; synthesise with history as context; keep both turns.
    The budget is spent by the router before this runs, and the configuration gate
    (`answers_configured`) sits before that — a deployment without an answer model
    refuses for free, exactly as `/v1/ask` does.

    What is stored: the question, the answer (or the refusal sentence the UI shows), and
    the citations as references. What is not: the passages. Replay re-checks the ACL.
    """
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    package_id: UUID = row.id  # type: ignore[attr-defined]
    await _touch_activity(session, package_id=package_id)

    history: list[Turn] = []
    if conversation_id is not None:
        await _owned_conversation(
            session, package_id=package_id, user_id=user_id, conversation_id=conversation_id
        )
        history = await _history(session, conversation_id=conversation_id)
    else:
        conversation_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO kt_conversations (id, org_id, kt_package_id, user_id, title) "
                "VALUES (:id, :org, :pkg, :user, :title)"
            ),
            {
                "id": conversation_id,
                "org": str(org_id),
                "pkg": package_id,
                "user": user_id,
                # The first question, trimmed. Never generated.
                "title": question.strip()[:200],
            },
        )

    vector, query_tokens = await embedder.embed(question)
    page = await search_chunks(
        session, user_id=user_id, query_vector=vector, k=k, within=_window(row)
    )
    outcome = await synthesise_answer(
        transport, question=question, evidence=list(page.items), history=history
    )

    citations_json = [
        {
            "marker": c.marker,
            "chunk_id": c.chunk_id,
            "document_id": c.document_id,
            "document_title": c.document_title,
            "source_system": c.source_system,
        }
        for c in outcome.citations
    ]
    question_id, answer_id = uuid4(), uuid4()
    await session.execute(
        text(
            # `clock_timestamp()`, not the column's `now()` default: both turns land in one
            # transaction, and `now()` is the transaction's start — identical for both
            # rows, leaving the user/assistant order to a random UUID tie-break. The wall
            # clock advances between the two statements, so the order is the order.
            "INSERT INTO kt_messages (id, org_id, conversation_id, role, content, created_at) "
            "VALUES (:id, :org, :c, 'user', :content, clock_timestamp())"
        ),
        {"id": question_id, "org": str(org_id), "c": conversation_id, "content": question},
    )
    await session.execute(
        text(
            "INSERT INTO kt_messages (id, org_id, conversation_id, role, content, "
            "citations_json, insufficient_evidence, attempts, created_at) "
            "VALUES (:id, :org, :c, 'assistant', :content, cast(:cites AS jsonb), "
            ":insufficient, :attempts, clock_timestamp())"
        ),
        {
            "id": answer_id,
            "org": str(org_id),
            "c": conversation_id,
            "content": outcome.answer
            if outcome.answer is not None
            else "The evidence you are authorised to read does not answer this.",
            "cites": json.dumps(citations_json),
            "insufficient": outcome.insufficient_evidence,
            "attempts": outcome.attempts,
        },
    )
    await session.execute(
        text("UPDATE kt_conversations SET updated_at = now() WHERE id = :id"),
        {"id": conversation_id},
    )
    # Counts only. The question is user-authored text and never enters the trail (§4.9).
    await _audit(
        session,
        org_id=org_id,
        actor_id=user_id,
        action="kt.copilot_asked",
        resource_id=package_id,
        correlation_id=correlation_id,
        meta={
            "conversation_id": str(conversation_id),
            "attempts": outcome.attempts,
            "insufficient_evidence": outcome.insufficient_evidence,
            "citations": len(outcome.citations),
            "sources": len(page.items),
            "query_tokens": query_tokens,
        },
    )

    # Freshly retrieved, so every cited document is visible by construction.
    citations = [
        StoredCitation(
            marker=c.marker,
            chunk_id=UUID(c.chunk_id),
            document_id=UUID(c.document_id),
            document_title=c.document_title,
            source_system=c.source_system,
            available=True,
        )
        for c in outcome.citations
    ]
    return CopilotTurn(
        conversation_id=conversation_id,
        question_message_id=question_id,
        answer_message_id=answer_id,
        answer=outcome.answer,
        insufficient_evidence=outcome.insufficient_evidence,
        citations=citations,
        sources=list(page.items),
        attempts=outcome.attempts,
        query_tokens=query_tokens,
    )


# ----------------------------------------------------------------------- bookmarks


async def _claim_visible(
    session: AsyncSession,
    *,
    row: object,
    claim_id: UUID,
    principals: frozenset[str],
    groups: frozenset[str],
) -> bool:
    """Whether one claim is inside the window, current, and readable — the same three
    gates `kt_insights` runs, on one row."""
    params: dict[str, object] = {
        "id": claim_id,
        "principals": list(principals),
        "groups": list(groups),
    }
    filters = ["cl.id = :id", "d.superseded_by IS NULL", *_period_filters(row, params)]
    found = (
        await session.execute(
            text(
                "SELECT 1 FROM extraction_claims cl "  # noqa: S608
                "JOIN chunks ch ON ch.id = cl.chunk_id "
                "JOIN documents d ON d.id = ch.document_id "
                + _LATEST_RUN_JOIN
                + f"WHERE {ACL_PREDICATE} AND {' AND '.join(filters)}"
            ),
            params,
        )
    ).first()
    return found is not None


async def _document_visible(
    session: AsyncSession,
    *,
    row: object,
    document_id: UUID,
    principals: frozenset[str],
    groups: frozenset[str],
) -> bool:
    params: dict[str, object] = {
        "id": document_id,
        "principals": list(principals),
        "groups": list(groups),
    }
    filters = ["d.id = :id", "d.superseded_by IS NULL", *_period_filters(row, params)]
    found = (
        await session.execute(
            text(
                "SELECT 1 FROM documents d "  # noqa: S608
                f"WHERE {ACL_PREDICATE} AND {' AND '.join(filters)}"
            ),
            params,
        )
    ).first()
    return found is not None


async def add_bookmark(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    kind: str,
    ref_id: UUID | None,
    note: str | None,
    principals: frozenset[str],
    groups: frozenset[str],
    correlation_id: str | None = None,
) -> BookmarkView:
    """Save a claim, document, message or question.

    A claim or document is checked for visibility BEFORE it is saved, with the same
    gates the tabs run. Saving is otherwise a way to learn whether an id exists: a
    bookmark that succeeds on an invisible claim confirms the claim. The refusal is the
    same 404 for "no such claim" and "not yours to read".
    """
    if kind not in BOOKMARK_KINDS:
        raise ValidationFailed(f"Unknown bookmark kind. One of: {', '.join(BOOKMARK_KINDS)}.")
    cleaned_note = note.strip() if note and note.strip() else None
    if kind == "question":
        if ref_id is not None or cleaned_note is None:
            raise ValidationFailed("A saved question is its text, and nothing else.")
    elif ref_id is None:
        raise ValidationFailed("A saved item needs the id of what it points at.")

    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    package_id: UUID = row.id  # type: ignore[attr-defined]
    await _touch_activity(session, package_id=package_id)

    if kind == "claim" and ref_id is not None:
        if not await _claim_visible(
            session, row=row, claim_id=ref_id, principals=principals, groups=groups
        ):
            raise NotFound(_REF_NOT_FOUND)
    elif kind == "document" and ref_id is not None:
        if not await _document_visible(
            session, row=row, document_id=ref_id, principals=principals, groups=groups
        ):
            raise NotFound(_REF_NOT_FOUND)
    elif kind == "message" and ref_id is not None:
        owned = (
            await session.execute(
                text(
                    "SELECT 1 FROM kt_messages m JOIN kt_conversations c "
                    "ON c.id = m.conversation_id "
                    "WHERE m.id = :id AND c.kt_package_id = :pkg AND c.user_id = :user"
                ),
                {"id": ref_id, "pkg": package_id, "user": user_id},
            )
        ).first()
        if owned is None:
            raise NotFound(_REF_NOT_FOUND)

    if ref_id is not None:
        # One bookmark per referent; a second save updates the note rather than
        # duplicating the row (the partial unique index in 0019 is the arbiter).
        bookmark_id = (
            await session.execute(
                text(
                    "INSERT INTO kt_bookmarks (id, org_id, kt_package_id, user_id, kind, "
                    "ref_id, note) VALUES (:id, :org, :pkg, :user, :kind, :ref, :note) "
                    "ON CONFLICT (kt_package_id, user_id, kind, ref_id) "
                    "WHERE ref_id IS NOT NULL "
                    "DO UPDATE SET note = EXCLUDED.note, updated_at = now() "
                    "RETURNING id"
                ),
                {
                    "id": uuid4(),
                    "org": str(org_id),
                    "pkg": package_id,
                    "user": user_id,
                    "kind": kind,
                    "ref": ref_id,
                    "note": cleaned_note,
                },
            )
        ).scalar_one()
    else:
        bookmark_id = (
            await session.execute(
                text(
                    "INSERT INTO kt_bookmarks (id, org_id, kt_package_id, user_id, kind, note) "
                    "VALUES (:id, :org, :pkg, :user, 'question', :note) RETURNING id"
                ),
                {
                    "id": uuid4(),
                    "org": str(org_id),
                    "pkg": package_id,
                    "user": user_id,
                    "note": cleaned_note,
                },
            )
        ).scalar_one()

    await _audit(
        session,
        org_id=org_id,
        actor_id=user_id,
        action="kt.bookmarked",
        resource_id=package_id,
        correlation_id=correlation_id,
        meta={"kind": kind},
    )
    items = await list_bookmarks(
        session,
        org_id=org_id,
        user_id=user_id,
        kt_code=kt_code,
        principals=principals,
        groups=groups,
    )
    for item in items:
        if item.id == bookmark_id:
            return item
    raise NotFound(_BOOKMARK_NOT_FOUND)  # pragma: no cover - written this transaction


async def list_bookmarks(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    principals: frozenset[str],
    groups: frozenset[str],
) -> list[BookmarkView]:
    """The recipient's bookmarks, each re-resolved under today's ACL.

    A claim or document the caller can no longer read renders `available=False` with a
    neutral label — the bookmark is theirs, the thing it pointed at is not.
    """
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    package_id: UUID = row.id  # type: ignore[attr-defined]

    rows = (
        await session.execute(
            text(
                "SELECT id, kind, ref_id, note, created_at, updated_at FROM kt_bookmarks "
                "WHERE kt_package_id = :pkg AND user_id = :user "
                "ORDER BY created_at DESC, id DESC LIMIT 500"
            ),
            {"pkg": package_id, "user": user_id},
        )
    ).all()
    if not rows:
        return []

    claim_ids = [r.ref_id for r in rows if r.kind == "claim" and r.ref_id is not None]
    document_ids = [r.ref_id for r in rows if r.kind == "document" and r.ref_id is not None]
    message_ids = [r.ref_id for r in rows if r.kind == "message" and r.ref_id is not None]

    claims: dict[UUID, tuple[str, str, str]] = {}
    if claim_ids:
        params: dict[str, object] = {
            "ids": [str(c) for c in claim_ids],
            "principals": list(principals),
            "groups": list(groups),
        }
        filters = ["cl.id = ANY(:ids)", "d.superseded_by IS NULL", *_period_filters(row, params)]
        for c in (
            await session.execute(
                text(
                    "SELECT cl.id, cl.claim_type, cl.payload_json, d.title "
                    "FROM extraction_claims cl "
                    "JOIN chunks ch ON ch.id = cl.chunk_id "
                    "JOIN documents d ON d.id = ch.document_id "
                    + _LATEST_RUN_JOIN
                    + f"WHERE {ACL_PREDICATE} AND {' AND '.join(filters)}"
                ),
                params,
            )
        ).all():
            payload = c.payload_json or {}
            label = payload.get("name") or payload.get("summary") or payload.get("quote") or ""
            claims[UUID(str(c.id))] = (str(label)[:160], str(c.title), str(c.claim_type))

    documents: dict[UUID, str] = {}
    if document_ids:
        params = {
            "ids": [str(d) for d in document_ids],
            "principals": list(principals),
            "groups": list(groups),
        }
        filters = ["d.id = ANY(:ids)", "d.superseded_by IS NULL", *_period_filters(row, params)]
        for d in (
            await session.execute(
                text(
                    "SELECT d.id, d.title FROM documents d "  # noqa: S608
                    f"WHERE {ACL_PREDICATE} AND {' AND '.join(filters)}"
                ),
                params,
            )
        ).all():
            documents[UUID(str(d.id))] = str(d.title)

    messages: dict[UUID, str] = {}
    if message_ids:
        for m in (
            await session.execute(
                text(
                    "SELECT m.id, m.content FROM kt_messages m "
                    "JOIN kt_conversations c ON c.id = m.conversation_id "
                    "WHERE m.id = ANY(:ids) AND c.kt_package_id = :pkg AND c.user_id = :user"
                ),
                {"ids": [str(m) for m in message_ids], "pkg": package_id, "user": user_id},
            )
        ).all():
            messages[UUID(str(m.id))] = str(m.content)[:160]

    out: list[BookmarkView] = []
    for r in rows:
        ref = UUID(str(r.ref_id)) if r.ref_id is not None else None
        label, available, tab = "No longer available to you", False, None
        if r.kind == "question":
            label, available = str(r.note or ""), True
        elif r.kind == "claim" and ref in claims:
            text_label, title, claim_type = claims[ref]
            label, available, tab = f"{text_label} · {title}", True, _TAB_FOR_TYPE.get(claim_type)
        elif r.kind == "document" and ref in documents:
            label, available, tab = documents[ref], True, "documents"
        elif r.kind == "message" and ref in messages:
            label, available, tab = messages[ref], True, "ask"
        out.append(
            BookmarkView(
                id=r.id,
                kind=r.kind,
                ref_id=ref,
                note=r.note,
                label=label,
                available=available,
                tab=tab,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
        )
    return out


async def remove_bookmark(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    bookmark_id: UUID,
) -> None:
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    deleted = (
        await session.execute(
            text(
                "DELETE FROM kt_bookmarks WHERE id = :id AND kt_package_id = :pkg "
                "AND user_id = :user RETURNING id"
            ),
            {"id": bookmark_id, "pkg": row.id, "user": user_id},  # type: ignore[attr-defined]
        )
    ).scalar_one_or_none()
    if deleted is None:
        raise NotFound(_BOOKMARK_NOT_FOUND)


# ------------------------------------------------------------------------ progress


async def list_progress(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, kt_code: str
) -> list[ProgressItem]:
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    rows = (
        await session.execute(
            text(
                "SELECT item_key, state, updated_at FROM kt_progress "
                "WHERE kt_package_id = :pkg AND user_id = :user ORDER BY updated_at DESC"
            ),
            {"pkg": row.id, "user": user_id},  # type: ignore[attr-defined]
        )
    ).all()
    return [ProgressItem(item_key=r.item_key, state=r.state, updated_at=r.updated_at) for r in rows]


async def set_progress(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    item_key: str,
    state: str,
) -> ProgressItem:
    """Mark an item seen, done or unclear. A marker — the item itself is never copied."""
    if state not in PROGRESS_STATES:
        raise ValidationFailed(f"Unknown state. One of: {', '.join(PROGRESS_STATES)}.")
    if not _ITEM_KEY.match(item_key):
        raise ValidationFailed("That is not an item this workspace tracks.")
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    await _touch_activity(session, package_id=row.id)  # type: ignore[attr-defined]
    updated_at = (
        await session.execute(
            text(
                "INSERT INTO kt_progress (org_id, kt_package_id, user_id, item_key, state) "
                "VALUES (:org, :pkg, :user, :key, :state) "
                "ON CONFLICT (kt_package_id, user_id, item_key) "
                "DO UPDATE SET state = EXCLUDED.state, updated_at = now() "
                "RETURNING updated_at"
            ),
            {
                "org": str(org_id),
                "pkg": row.id,  # type: ignore[attr-defined]
                "user": user_id,
                "key": item_key,
                "state": state,
            },
        )
    ).scalar_one()
    return ProgressItem(item_key=item_key, state=state, updated_at=updated_at)


async def clear_progress(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, kt_code: str, item_key: str
) -> None:
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    await session.execute(
        text(
            "DELETE FROM kt_progress WHERE kt_package_id = :pkg AND user_id = :user "
            "AND item_key = :key"
        ),
        {"pkg": row.id, "user": user_id, "key": item_key},  # type: ignore[attr-defined]
    )


# ----------------------------------------------------------------------- workspace

#: The learning path, stage by stage. Each stage names the claim type it draws from and
#: the tab it lives on; a stage whose category is outside the package's scope, or which
#: has nothing visible, is simply absent — the path is as long as the evidence.
_STAGES: Final[tuple[tuple[int, str, str, str], ...]] = (
    (1, "Understand your responsibility", "responsibility", "responsibilities"),
    (2, "Know the projects", "project", "projects"),
    (3, "Understand the decisions", "decision", "decisions"),
    (7, "Know the people", "person", "people"),
    (14, "Catch up on the meetings", "meeting", "meetings"),
)
_STAGE_ITEMS: Final = 5
_RECENT_DOCUMENTS: Final = 5


async def _coverage(
    session: AsyncSession,
    *,
    row: object,
    scope: list[str],
    principals: frozenset[str],
    groups: frozenset[str],
    by_type: dict[str, int],
) -> Coverage:
    params: dict[str, object] = {"principals": list(principals), "groups": list(groups)}
    filters = ["d.superseded_by IS NULL", *_period_filters(row, params)]
    totals = (
        await session.execute(
            text(
                "SELECT count(*) AS documents_visible, "  # noqa: S608
                "count(r.id) AS documents_extracted, "
                "COALESCE(SUM((r.stats_json->>'chunks_covered')::int), 0) AS chunks_covered, "
                "COALESCE(SUM((r.stats_json->>'chunks_total')::int), 0) AS chunks_total "
                "FROM documents d "
                "LEFT JOIN LATERAL ("
                "  SELECT r2.id, r2.stats_json FROM extraction_runs r2 "
                "  WHERE r2.stats_json->>'document_id' = d.id::text "
                "  AND r2.finished_at IS NOT NULL "
                "  ORDER BY r2.started_at DESC LIMIT 1"
                ") r ON true "
                f"WHERE {ACL_PREDICATE} AND {' AND '.join(filters)}"
            ),
            params,
        )
    ).one()
    documents_visible = int(totals.documents_visible)
    documents_extracted = int(totals.documents_extracted)
    chunks_covered = int(totals.chunks_covered)
    chunks_total = int(totals.chunks_total)

    ratio: float | None = None
    reliable = documents_visible > 0 and documents_extracted > 0 and chunks_total > 0
    if reliable:
        ratio = round(min(1.0, chunks_covered / chunks_total), 3)
        reason = (
            "Computed from the documents your account may read inside this package's "
            "window and the latest extraction run over each of them."
        )
    elif documents_visible == 0:
        reason = (
            "Coverage cannot be calculated reliably yet: no documents in this package's "
            "window are readable by your account."
        )
    else:
        reason = (
            "Coverage cannot be calculated reliably yet: extraction has not run over the "
            "documents you can read."
        )

    categories = [
        CoverageCategory(
            category=category, claim_type=claim_type, claims_visible=by_type.get(claim_type, 0)
        )
        for claim_type, category in _CLAIM_SCOPE.items()
        if category in scope
    ]
    return Coverage(
        categories=categories,
        documents_visible=documents_visible,
        documents_extracted=documents_extracted,
        chunks_covered=chunks_covered,
        chunks_total=chunks_total,
        extraction_ratio=ratio,
        reliable=reliable,
        reason=reason,
    )


async def read_workspace(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    principals: frozenset[str],
    groups: frozenset[str],
) -> Workspace:
    """Coverage, the learning path, what to do next, what is still unclear, and where
    the recipient left off — one call, so the overview is one round trip.

    Everything is derived from the recipient's visible evidence at this moment. Nothing
    is stored except the recipient's own progress markers, so a revoked grant changes
    the whole workspace on the next load.
    """
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    package_id: UUID = row.id  # type: ignore[attr-defined]
    scope = list(row.scope)  # type: ignore[attr-defined]
    await _touch_activity(session, package_id=package_id)

    summary = await kt_insight_summary(
        session,
        org_id=org_id,
        user_id=user_id,
        kt_code=kt_code,
        principals=principals,
        groups=groups,
    )
    coverage = await _coverage(
        session, row=row, scope=scope, principals=principals, groups=groups, by_type=summary.by_type
    )

    progress = {
        item.item_key: item.state
        for item in await list_progress(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    }

    # ---- learning path
    stages: list[LearningStage] = []
    if "profile" in scope:
        stages.append(
            LearningStage(
                day=1,
                title="Understand your responsibility",
                items=[
                    LearningItem(
                        key="step:profile",
                        kind="step",
                        label="Who you are taking over from",
                        why="Their practice, title and level as the organisation records them.",
                        tab="",
                        ref_id=None,
                        state=progress.get("step:profile"),
                    )
                ],
            )
        )
    recent_decisions: list[KtInsight] = []
    for day, title, claim_type, tab in _STAGES:
        category = _CLAIM_SCOPE[claim_type]
        if category not in scope or summary.by_type.get(claim_type, 0) == 0:
            continue
        insights = await kt_insights(
            session,
            org_id=org_id,
            user_id=user_id,
            kt_code=kt_code,
            principals=principals,
            groups=groups,
            claim_type=claim_type,
            limit=_STAGE_ITEMS if claim_type != "decision" else 10,
        )
        if claim_type == "decision":
            recent_decisions = insights
            insights = insights[:_STAGE_ITEMS]
        items = [
            LearningItem(
                key=f"claim:{i.id}",
                kind=claim_type,
                label=_label_for_claim(i),
                why=(
                    _why_for_claim(i)
                    if claim_type != "person"
                    else f"Most recently mentioned · {_why_for_claim(i)}"
                ),
                tab=tab,
                ref_id=i.id,
                state=progress.get(f"claim:{i.id}"),
            )
            for i in insights
        ]
        if not items:
            continue
        if stages and stages[0].day == 1 and day == 1:
            stages[0] = LearningStage(day=1, title=title, items=[*stages[0].items, *items])
        else:
            stages.append(LearningStage(day=day, title=title, items=items))

    recent_documents = []
    if "documents" in scope:
        page = await kt_documents(
            session,
            org_id=org_id,
            user_id=user_id,
            kt_code=kt_code,
            principals=principals,
            groups=groups,
            limit=_RECENT_DOCUMENTS,
            cursor=None,
        )
        recent_documents = page.items
        if recent_documents:
            stages.append(
                LearningStage(
                    day=30,
                    title="Read the source material",
                    items=[
                        LearningItem(
                            key=f"document:{d.id}",
                            kind="document",
                            label=d.title,
                            why=f"{d.source_system} · {d.created_at.strftime('%Y-%m-%d')}",
                            tab="documents",
                            ref_id=d.id,
                            state=progress.get(f"document:{d.id}"),
                        )
                        for d in recent_documents
                    ],
                )
            )

    all_items = [item for stage in stages for item in stage.items]
    path_total = len(all_items)
    path_done = sum(1 for item in all_items if item.state == "done")

    # ---- recommendations: what to do next, each with its reason
    recommendations: list[Recommendation] = []
    next_item = next((i for i in all_items if i.state not in ("done", "seen")), None)
    if next_item is not None:
        recommendations.append(
            Recommendation(
                key=next_item.key,
                kind="path",
                label=next_item.label,
                why=(
                    "Next on your learning path."
                    if path_done
                    else "Where your learning path starts."
                ),
                tab=next_item.tab,
                ref_id=next_item.ref_id,
            )
        )
    unclear_keys = [k for k, s in progress.items() if s == "unclear"]
    labels_by_key = {i.key: i for i in all_items}
    for key in unclear_keys[:2]:
        item = labels_by_key.get(key)
        recommendations.append(
            Recommendation(
                key=key,
                kind="unclear",
                label=item.label if item else "Something you marked unclear",
                why="You marked this unclear. Ask JUTSU about it, or mark it understood.",
                tab=item.tab if item else "ask",
                ref_id=item.ref_id if item else None,
            )
        )
    for decision in recent_decisions:
        key = f"claim:{decision.id}"
        if key not in progress and decision.confidence >= 0.8 and len(recommendations) < 5:
            recommendations.append(
                Recommendation(
                    key=key,
                    kind="decision",
                    label=_label_for_claim(decision),
                    why=f"A recent decision you have not reviewed · {_why_for_claim(decision)}",
                    tab="decisions",
                    ref_id=decision.id,
                )
            )
            break
    for d in recent_documents:
        key = f"document:{d.id}"
        if key not in progress and len(recommendations) < 5:
            recommendations.append(
                Recommendation(
                    key=key,
                    kind="document",
                    label=d.title,
                    why=f"Recent material in the window you have not opened · {d.source_system}",
                    tab="documents",
                    ref_id=d.id,
                )
            )
            break

    # ---- gaps: what the recipient flagged, and what the evidence itself lacks
    gaps: list[Gap] = []
    for key in unclear_keys:
        item = labels_by_key.get(key)
        gaps.append(
            Gap(
                key=key,
                label=item.label if item else key,
                why="You marked this unclear.",
                source="you",
                tab=item.tab if item else None,
                ref_id=item.ref_id if item else None,
            )
        )
    for bucket in coverage.categories:
        if bucket.claims_visible == 0:
            gaps.append(
                Gap(
                    key=f"category:{bucket.category}",
                    label=f"No {bucket.category} evidence is visible to you yet",
                    why=(
                        "Extraction has not run over the documents you can read."
                        if coverage.documents_extracted == 0
                        else "Nothing extracted in this window is readable by your account."
                    ),
                    source="evidence",
                    tab=_TAB_FOR_TYPE.get(bucket.claim_type),
                    ref_id=None,
                )
            )
    if "documents" in scope and coverage.documents_visible == 0:
        gaps.append(
            Gap(
                key="category:documents",
                label="No documents in this window are readable by your account",
                why=(
                    "Access comes from your linked source identities; the package cannot widen it."
                ),
                source="evidence",
                tab="documents",
                ref_id=None,
            )
        )

    # ---- resume
    conversations = await list_conversations(
        session, org_id=org_id, user_id=user_id, kt_code=kt_code, limit=1, cursor=None
    )
    bookmark_count = (
        await session.execute(
            text(
                "SELECT count(*) FROM kt_bookmarks WHERE kt_package_id = :pkg AND user_id = :user"
            ),
            {"pkg": package_id, "user": user_id},
        )
    ).scalar_one()
    resume = ResumeCard(
        last_conversation=conversations.items[0] if conversations.items else None,
        last_activity_at=getattr(row, "last_activity_at", None),
        bookmarks=int(bookmark_count),
        unclear=len(unclear_keys),
        path_done=path_done,
        path_total=path_total,
    )

    return Workspace(
        coverage=coverage,
        learning_path=stages,
        recommendations=recommendations,
        gaps=gaps,
        resume=resume,
    )
