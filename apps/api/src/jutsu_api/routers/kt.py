"""Knowledge transfer over HTTP: the admin lifecycle and the recipient's window.

  kt:manage  POST /v1/kt · GET /v1/kt · GET /v1/kt/{id} · revoke · complete
  kt:open    POST /v1/kt/claim · GET /v1/kt/{code}/documents

The recipient's Ask experience is the ordinary `POST /v1/search` under their own
authorization — deliberately not a KT-specific search endpoint, because a second search
path is a second place an ACL bug can live (§12).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status
from jutsu_core.rbac import Permission
from pydantic import BaseModel, EmailStr, Field

from jutsu_api.auth_service import scoped_acl_principals
from jutsu_api.deps import CurrentPrincipal, Db
from jutsu_api.kt import (
    SUPPORTED_SCOPES,
    claim_or_open,
    complete_package,
    create_package,
    get_package,
    kt_documents,
    list_packages,
    revoke_package,
)
from jutsu_api.security import GuardedAPIRoute, requires

router = APIRouter(prefix="/v1", tags=["kt"], route_class=GuardedAPIRoute)


class SubjectProfileOut(BaseModel):
    display_name: str | None
    designation: str | None
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


class KtDocumentOut(BaseModel):
    id: UUID
    title: str
    source_system: str
    created_at: datetime


class KtDocumentPageOut(BaseModel):
    items: list[KtDocumentOut]
    next_cursor: str | None


# ------------------------------------------------------------------------- admin


@router.get("/kt/scopes")
@requires(Permission.KT_MANAGE)
async def read_supported_scopes(principal: CurrentPrincipal, session: Db) -> KtScopesOut:
    return KtScopesOut(supported=list(SUPPORTED_SCOPES))


@router.post("/kt", status_code=status.HTTP_201_CREATED)
@requires(Permission.KT_MANAGE)
async def create(payload: KtCreatePayload, principal: CurrentPrincipal, session: Db) -> KtAdminOut:
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
async def revoke(package_id: UUID, principal: CurrentPrincipal, session: Db) -> KtAdminOut:
    """Revoke. Takes effect at the next authorization check, which is every check —
    the workspace stops answering whatever any browser has cached (§39)."""
    view = await revoke_package(
        session, org_id=principal.org_id, actor_id=principal.user_id, package_id=package_id
    )
    return KtAdminOut(**asdict(view))


@router.post("/kt/{package_id}/complete")
@requires(Permission.KT_MANAGE)
async def complete(package_id: UUID, principal: CurrentPrincipal, session: Db) -> KtAdminOut:
    view = await complete_package(
        session, org_id=principal.org_id, actor_id=principal.user_id, package_id=package_id
    )
    return KtAdminOut(**asdict(view))


# ---------------------------------------------------------------------- recipient


@router.post("/kt/claim")
@requires(Permission.KT_OPEN)
async def claim(
    payload: KtClaimPayload, principal: CurrentPrincipal, session: Db
) -> KtRecipientOut:
    """Open a package addressed to you, claiming it on first open.

    Every refusal is server-side and specific where it is safe to be (revoked, expired)
    and deliberately uniform where it is not: a foreign tenant's code, a typo and a
    package bound to someone else all answer with the same 404.
    """
    view = await claim_or_open(
        session, org_id=principal.org_id, user_id=principal.user_id, kt_code=payload.kt_code
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
