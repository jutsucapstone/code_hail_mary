"""Knowledge transfer over HTTP: the admin lifecycle and the recipient's window.

  kt:manage  POST /v1/kt · GET /v1/kt · GET /v1/kt/{id} · revoke · complete
  kt:open    POST /v1/kt/claim · GET /v1/kt/{code}/documents · …/documents/{document_id}

The recipient's Ask experience is the ordinary `POST /v1/search` under their own
authorization — deliberately not a KT-specific search endpoint, because a second search
path is a second place an ACL bug can live (§12).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from jutsu_core.errors import ServiceUnavailable
from jutsu_core.rbac import Permission
from pydantic import BaseModel, EmailStr, Field

from jutsu_api.answers import answers_configured
from jutsu_api.auth_service import scoped_acl_principals
from jutsu_api.deps import CurrentPrincipal, Db, StoreDep
from jutsu_api.kt import (
    SUPPORTED_SCOPES,
    claim_or_open,
    complete_package,
    create_package,
    get_package,
    kt_document,
    kt_documents,
    kt_handover_summary,
    kt_insight_summary,
    kt_insights,
    list_packages,
    revoke_package,
    update_package,
)
from jutsu_api.kt_files import (
    attach_files,
    attachable_files,
    detach_file,
    package_attachments,
    shared_download_url,
    shared_files,
)
from jutsu_api.rate_limit import Bucket, spend_budget
from jutsu_api.routers.search import AnswerTransportDep
from jutsu_api.security import GuardedAPIRoute, requires

router = APIRouter(prefix="/v1", tags=["kt"], route_class=GuardedAPIRoute)


class SubjectProfileOut(BaseModel):
    display_name: str | None
    designation: str | None
    #: Under the same `profile` scope as `designation`. The normalized level is what
    #: makes the subject's seniority comparable with anyone else's; the title is what
    #: they were actually called.
    practice: str | None = None
    role_title: str | None = None
    role_level: str | None = None
    department: str | None


class KtRecipientOut(BaseModel):
    kt_code: str
    status: str
    scope: list[str]
    period_start: datetime | None
    period_end: datetime | None
    expires_at: datetime
    created_at: datetime
    subject: SubjectProfileOut


class KtAdminOut(BaseModel):
    id: UUID
    kt_code: str
    subject_user_id: UUID
    subject_name: str | None
    subject_email: str
    status: str
    scope: list[str]
    period_start: datetime | None
    period_end: datetime | None
    expires_at: datetime
    recipient_email: str | None
    claimed_at: datetime | None
    created_at: datetime
    last_activity_at: datetime | None


class KtAdminPageOut(BaseModel):
    items: list[KtAdminOut]
    next_cursor: str | None


class KtScopesOut(BaseModel):
    #: What the create wizard may offer. Served by the backend so the UI cannot invent
    #: a category the platform cannot fill (§13).
    supported: list[str]


class KtCreatePayload(BaseModel):
    model_config = {"extra": "forbid"}

    subject_user_id: UUID
    scope: list[str] = Field(min_length=1, max_length=8)
    validity_days: int = Field(ge=1, le=365)
    #: How far back the package looks. Omitted means the subject's whole history.
    period_days: int | None = Field(default=None, ge=1, le=3650)
    #: Bind the package to one address up front. Omitted, the first eligible opener
    #: claims it — after which it is bound anyway.
    recipient_email: EmailStr | None = None


class KtClaimPayload(BaseModel):
    model_config = {"extra": "forbid"}

    kt_code: str = Field(min_length=8, max_length=24)


class KtUpdatePayload(BaseModel):
    """The two edits an administrator may make after creation.

    `extend_days` counts from the later of now and the current expiry, never past a year
    from today. `recipient_email` re-addresses a package nobody has claimed; a claimed
    one refuses with 409. Neither field touches scope or period — those describe what
    the package IS, and changing them under a recipient's feet would make the workspace
    they were reading a different one.
    """

    model_config = {"extra": "forbid"}

    extend_days: int | None = Field(default=None, ge=1, le=365)
    recipient_email: EmailStr | None = None


class KtDocumentOut(BaseModel):
    id: UUID
    title: str
    source_system: str
    created_at: datetime


class KtDocumentPageOut(BaseModel):
    items: list[KtDocumentOut]
    next_cursor: str | None


class KtDocumentChunkOut(BaseModel):
    ordinal: int
    #: The MASKED passage, in document order. No character offsets travel with it: the
    #: stored pair indexes the ORIGINAL body, and offering them against this string is the
    #: mis-highlight trap. A span belongs to `/v1/evidence/{chunk_id}`.
    text: str


class KtDocumentDetailOut(BaseModel):
    id: UUID
    title: str
    source_system: str
    created_at: datetime
    chunks: list[KtDocumentChunkOut]
    total_chunks: int
    #: `from_ordinal` for the next request, or null at the end of the document.
    next_ordinal: int | None


# ------------------------------------------------------------------------- admin


@router.get("/kt/scopes")
@requires(Permission.KT_MANAGE)
async def read_supported_scopes(principal: CurrentPrincipal, session: Db) -> KtScopesOut:
    return KtScopesOut(supported=list(SUPPORTED_SCOPES))


@router.post("/kt", status_code=status.HTTP_201_CREATED)
@requires(Permission.KT_MANAGE)
async def create(
    payload: KtCreatePayload, principal: CurrentPrincipal, session: Db, request: Request
) -> KtAdminOut:
    """Create a package. Creates no access: what the recipient reads inside it is
    bounded by their own grants, per query, exactly as everywhere else."""
    view = await create_package(
        session,
        org_id=principal.org_id,
        created_by=principal.user_id,
        subject_user_id=payload.subject_user_id,
        scope=payload.scope,
        validity_days=payload.validity_days,
        period_days=payload.period_days,
        recipient_email=str(payload.recipient_email) if payload.recipient_email else None,
        correlation_id=request.state.request_id,
    )
    return KtAdminOut(**asdict(view))


@router.get("/kt")
@requires(Permission.KT_MANAGE)
async def read_packages(
    principal: CurrentPrincipal,
    session: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
) -> KtAdminPageOut:
    page = await list_packages(session, limit=limit, cursor=cursor)
    return KtAdminPageOut(
        items=[KtAdminOut(**asdict(item)) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/kt/{package_id}")
@requires(Permission.KT_MANAGE)
async def read_package(package_id: UUID, principal: CurrentPrincipal, session: Db) -> KtAdminOut:
    view = await get_package(session, package_id=package_id)
    return KtAdminOut(**asdict(view))


@router.post("/kt/{package_id}/revoke")
@requires(Permission.KT_MANAGE)
async def revoke(
    package_id: UUID, principal: CurrentPrincipal, session: Db, request: Request
) -> KtAdminOut:
    """Revoke. Takes effect at the next authorization check, which is every check —
    the workspace stops answering whatever any browser has cached (§39)."""
    view = await revoke_package(
        session,
        org_id=principal.org_id,
        actor_id=principal.user_id,
        package_id=package_id,
        correlation_id=request.state.request_id,
    )
    return KtAdminOut(**asdict(view))


@router.post("/kt/{package_id}/complete")
@requires(Permission.KT_MANAGE)
async def complete(
    package_id: UUID, principal: CurrentPrincipal, session: Db, request: Request
) -> KtAdminOut:
    view = await complete_package(
        session,
        org_id=principal.org_id,
        actor_id=principal.user_id,
        package_id=package_id,
        correlation_id=request.state.request_id,
    )
    return KtAdminOut(**asdict(view))


@router.patch("/kt/{package_id}")
@requires(Permission.KT_MANAGE)
async def update(
    package_id: UUID,
    payload: KtUpdatePayload,
    principal: CurrentPrincipal,
    session: Db,
    request: Request,
) -> KtAdminOut:
    """Extend a package's expiry, or re-address one nobody has claimed yet. Every change
    is its own audit row; a revoked or completed package refuses both."""
    view = await update_package(
        session,
        org_id=principal.org_id,
        actor_id=principal.user_id,
        package_id=package_id,
        extend_days=payload.extend_days,
        recipient_email=str(payload.recipient_email) if payload.recipient_email else None,
        correlation_id=request.state.request_id,
    )
    return KtAdminOut(**asdict(view))


# ---------------------------------------------------------------------- recipient


@router.post("/kt/claim")
@requires(Permission.KT_OPEN)
async def claim(
    payload: KtClaimPayload, principal: CurrentPrincipal, session: Db, request: Request
) -> KtRecipientOut:
    """Open a package addressed to you, claiming it on first open.

    Every refusal is server-side and specific where it is safe to be (revoked, expired)
    and deliberately uniform where it is not: a foreign tenant's code, a typo and a
    package bound to someone else all answer with the same 404.

    The budget is spent BEFORE the lookup, on its own committed session, so a refused
    guess costs the caller quota — that is what turns a 40-bit code space from "probe as
    fast as the API answers" into a wall. The denied-open audit rows remain the evidence
    of a probe; this is what stops one.
    """
    await spend_budget(Bucket.KT_CLAIM, org_id=principal.org_id, user_id=principal.user_id)
    view = await claim_or_open(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=payload.kt_code,
        correlation_id=request.state.request_id,
    )
    return KtRecipientOut(
        kt_code=view.kt_code,
        status=view.status,
        scope=view.scope,
        period_start=view.period_start,
        period_end=view.period_end,
        expires_at=view.expires_at,
        created_at=view.created_at,
        subject=SubjectProfileOut(**asdict(view.subject)),
    )


@router.get("/kt/{kt_code}/documents")
@requires(Permission.KT_OPEN)
async def read_kt_documents(
    kt_code: str,
    principal: CurrentPrincipal,
    session: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
) -> KtDocumentPageOut:
    """Documents inside the package window the RECIPIENT may already read.

    The ACL filter is retrieval's own predicate, inside the SQL, against the caller's
    principals resolved fresh for this request. The package contributes the period; it
    grants nothing.
    """
    principals, groups = await scoped_acl_principals(session, user_id=principal.user_id)
    page = await kt_documents(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        principals=principals,
        groups=groups,
        limit=limit,
        cursor=cursor,
    )
    return KtDocumentPageOut(
        items=[KtDocumentOut(**asdict(item)) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/kt/{kt_code}/documents/{document_id}")
@requires(Permission.KT_OPEN)
async def read_kt_document(
    kt_code: str,
    document_id: UUID,
    principal: CurrentPrincipal,
    session: Db,
    from_ordinal: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> KtDocumentDetailOut:
    """One document from the listing, opened: its masked passages in document order.

    The same permission and the same gates as the listing — `_open_for`, the package's
    scope, then retrieval's own predicate and the package's period ANDed together inside
    the SQL. A document that does not exist, one this recipient may not read and one
    outside the window are the identical 404; a closed package is the package's 403.

    Read a page at a time (`from_ordinal`, `next_ordinal`) because a document has no
    bounded size and a whole handbook in one response helps nobody.
    """
    principals, groups = await scoped_acl_principals(session, user_id=principal.user_id)
    detail = await kt_document(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        document_id=document_id,
        principals=principals,
        groups=groups,
        from_ordinal=from_ordinal,
        limit=limit,
    )
    return KtDocumentDetailOut(
        id=detail.id,
        title=detail.title,
        source_system=detail.source_system,
        created_at=detail.created_at,
        chunks=[KtDocumentChunkOut(ordinal=c.ordinal, text=c.text) for c in detail.chunks],
        total_chunks=detail.total_chunks,
        next_ordinal=detail.next_ordinal,
    )


class KtInsightOut(BaseModel):
    id: UUID
    claim_type: str
    summary: str | None
    name: str | None
    #: The date the passage itself stated, when it stated one. Never inferred.
    date: str | None
    #: The verbatim evidence, exactly as the quote gate verified it.
    quote: str
    confidence: float
    document_id: UUID
    document_title: str
    source_system: str
    chunk_id: UUID
    occurred_at: datetime


class KtInsightsOut(BaseModel):
    items: list[KtInsightOut]


class KtInsightSummaryOut(BaseModel):
    #: Counts per claim type, computed under the same ACL predicate that serves the
    #: rows — a count here can never exceed what the list would show.
    by_type: dict[str, int]


@router.get("/kt/{kt_code}/insights")
@requires(Permission.KT_OPEN)
async def read_kt_insights(
    kt_code: str,
    principal: CurrentPrincipal,
    session: Db,
    type: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> KtInsightsOut:
    """Extracted, quote-gated claims the RECIPIENT may read, in the package window.

    `type` filters to one claim type; omitted, it returns every type the package's
    scope covers, date-ordered — the timeline. Every row carries its verbatim quote and
    the chunk it anchors to, so a citation is one evidence fetch away.
    """
    principals, groups = await scoped_acl_principals(session, user_id=principal.user_id)
    items = await kt_insights(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        principals=principals,
        groups=groups,
        claim_type=type,
        limit=limit,
    )
    return KtInsightsOut(items=[KtInsightOut(**asdict(item)) for item in items])


@router.get("/kt/{kt_code}/insights-summary")
@requires(Permission.KT_OPEN)
async def read_kt_insight_summary(
    kt_code: str, principal: CurrentPrincipal, session: Db
) -> KtInsightSummaryOut:
    principals, groups = await scoped_acl_principals(session, user_id=principal.user_id)
    summary = await kt_insight_summary(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        principals=principals,
        groups=groups,
    )
    return KtInsightSummaryOut(by_type=summary.by_type)


class HandoverCitationOut(BaseModel):
    marker: int
    document_id: UUID
    document_title: str
    source_system: str


class HandoverSummaryOut(BaseModel):
    #: None when the claims could not ground a summary — the UI renders the refusal,
    #: never an empty string pretending to be one.
    summary: str | None
    insufficient_evidence: bool
    citations: list[HandoverCitationOut]
    attempts: int


@router.get("/kt/{kt_code}/handover-summary")
@requires(Permission.KT_OPEN)
async def read_kt_handover_summary(
    kt_code: str,
    principal: CurrentPrincipal,
    session: Db,
    transport: AnswerTransportDep,
) -> HandoverSummaryOut:
    """§29's executive summary: composed on demand from the recipient's own claim
    visibility, grounded and citation-gated exactly like /v1/ask, never persisted.

    Refuses before any spend when no answer model is configured — the same honest 503
    the ask surface gives, so the button in the KT console can say why.
    """
    if not answers_configured():
        raise ServiceUnavailable(
            "Handover summaries are not configured for this deployment yet. The "
            "knowledge tabs still work; a summary needs an answer model."
        )
    # After the free configuration gate and before anything paid — the same ordering as
    # /v1/ask. A summary is one model call per press; the budget is what stops a held key.
    await spend_budget(Bucket.KT_SUMMARY, org_id=principal.org_id, user_id=principal.user_id)
    principals, groups = await scoped_acl_principals(session, user_id=principal.user_id)
    outcome = await kt_handover_summary(
        session,
        transport,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        principals=principals,
        groups=groups,
    )
    return HandoverSummaryOut(
        summary=outcome.answer,
        insufficient_evidence=outcome.insufficient_evidence,
        citations=[
            HandoverCitationOut(
                marker=c.marker,
                document_id=UUID(str(c.document_id)),
                document_title=c.document_title,
                source_system=c.source_system,
            )
            for c in outcome.citations
        ],
        attempts=outcome.attempts,
    )


# ------------------------------------------------------ basket files on a package
#
# Two audiences on one table, and the split runs through every route below (ADR 0021).
#
# The RECIPIENT's two routes are `kt:open` and reach the package only through
# `open_package_for`, which re-decides binding, revocation, completion and expiry and
# spends a `KT_OPEN` allowance before it looks anything up. Revoking a package therefore
# closes its files in the same instant, with nothing running at revocation time.
#
# The CURATOR's four routes are `kt:open` at the decorator and `kt:manage`-or-subject in
# the service, and that asymmetry is deliberate rather than an omission. The departing
# employee may curate their own handover, and they hold no admin permission at all — so
# the decorator cannot express the rule and `_curatable` is the authorization. It answers
# 404 rather than 403 to a caller who is neither, so nothing here confirms that another
# employee's package exists.


class SharedFileOut(BaseModel):
    """One attached file, as its recipient sees it.

    No owner id, no object key, no failure reason. A recipient learns what the file is
    and whether its text is in the corpus; the machinery is the owner's business.
    """

    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    #: `ready` — its text is in the corpus and Ask KT can cite it — or `stored`, which is
    #: kept and downloadable and deliberately not searchable. Passed through in the
    #: owner's own vocabulary so the two consoles cannot describe one file differently.
    state: str
    extracted_chars: int | None
    uploaded_at: datetime
    attached_at: datetime


class SharedFilePageOut(BaseModel):
    items: list[SharedFileOut]


class KtFileDownloadOut(BaseModel):
    #: A short-lived signed URL. Returned as JSON rather than a 302 so the browser
    #: fetches it deliberately — a redirect to a signed URL lands in history, in
    #: referrer headers and in server logs.
    url: str


class AttachmentOut(BaseModel):
    """One file as the curator sees it: the same row, plus who put it there."""

    id: UUID
    file_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    state: str
    attached_at: datetime
    attached_by: UUID


class AttachmentPageOut(BaseModel):
    items: list[AttachmentOut]


class AttachRequest(BaseModel):
    #: Bounded because the picker is bounded. A larger set is a script, and a script
    #: attaching two hundred files in one request is a different feature.
    file_ids: Annotated[list[UUID], Field(min_length=1, max_length=100)]


class AttachedOut(BaseModel):
    #: How many were NEWLY attached. Lower than what was asked for when a file was
    #: already on the package, is not the subject's, or is not one this actor can see —
    #: the service refuses those silently rather than saying which failed why, because
    #: naming the reason per id is a probe of somebody else's basket.
    attached: int


@router.get("/kt/{kt_code}/files")
@requires(Permission.KT_OPEN)
async def read_kt_files(
    kt_code: str, principal: CurrentPrincipal, session: Db
) -> SharedFilePageOut:
    """The Knowledge Basket files shared with this package's recipient.

    Unpaginated on purpose: a handover attaches a curated handful, the picker caps at
    100, and a cursor over a list that size is machinery nobody needs.
    """
    rows = await shared_files(
        session, org_id=principal.org_id, user_id=principal.user_id, kt_code=kt_code
    )
    return SharedFilePageOut(items=[SharedFileOut(**asdict(row)) for row in rows])


@router.get("/kt/{kt_code}/files/{file_id}/download")
@requires(Permission.KT_OPEN)
async def read_kt_file_download(
    kt_code: str,
    file_id: UUID,
    principal: CurrentPrincipal,
    session: Db,
    store: StoreDep,
) -> KtFileDownloadOut:
    """A short-lived signed URL for one attached file.

    JSON rather than a 302, for the reason the basket's own download route gives: a
    redirect to a signed URL ends up in history, in referrer headers and in server logs.
    The grant is verified before the URL exists, never after.
    """
    url = await shared_download_url(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        kt_code=kt_code,
        store=store,
        file_id=file_id,
    )
    return KtFileDownloadOut(url=url)


@router.get("/kt/{package_id}/attachments")
@requires(Permission.KT_OPEN)
async def read_attachments(
    package_id: UUID, principal: CurrentPrincipal, session: Db
) -> AttachmentPageOut:
    """What this package currently shares. Authorized in the service, not the decorator."""
    rows = await package_attachments(session, actor=principal, package_id=package_id)
    return AttachmentPageOut(items=[AttachmentOut(**asdict(row)) for row in rows])


@router.get("/kt/{package_id}/attachable")
@requires(Permission.KT_OPEN)
async def read_attachable(
    package_id: UUID, principal: CurrentPrincipal, session: Db
) -> AttachmentPageOut:
    """The subject's files this caller could attach, minus the ones already on.

    Bounded by exactly the conditions the write enforces, so the picker cannot offer
    something the attach would then refuse — and a caller without `basket:manage` sees an
    empty list rather than a filtered view of somebody else's basket.
    """
    rows = await attachable_files(session, actor=principal, package_id=package_id)
    return AttachmentPageOut(items=[AttachmentOut(**asdict(row)) for row in rows])


@router.post("/kt/{package_id}/attachments", status_code=status.HTTP_201_CREATED)
@requires(Permission.KT_OPEN)
async def create_attachments(
    package_id: UUID, payload: AttachRequest, principal: CurrentPrincipal, session: Db
) -> AttachedOut:
    """Attach basket files to a package."""
    attached = await attach_files(
        session, actor=principal, package_id=package_id, file_ids=payload.file_ids
    )
    return AttachedOut(attached=attached)


@router.delete("/kt/{package_id}/attachments/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
@requires(Permission.KT_OPEN)
async def remove_attachment(
    package_id: UUID, file_id: UUID, principal: CurrentPrincipal, session: Db
) -> None:
    """Stop sharing one file. The file itself is untouched and stays the owner's."""
    await detach_file(session, actor=principal, package_id=package_id, file_id=file_id)
