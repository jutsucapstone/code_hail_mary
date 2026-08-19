"""Organisation registration — the entry point for a pilot.

Public, because there is nobody to authenticate yet: this route is what brings the first
account into existence.

The response is identical whether or not the domain was already registered. That is not
politeness, it is the difference between a sign-up form and a customer-enumeration
endpoint — without it, anyone could probe domains to learn which companies use JUTSU.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status
from jutsu_core.errors import NotFound
from jutsu_core.rbac import Permission
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text

from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import CurrentPrincipal, Db, get_email_sender
from jutsu_api.email import EmailSender
from jutsu_api.registration import RegistrationRequest, register_organisation
from jutsu_api.security import GuardedAPIRoute, public, requires

router = APIRouter(prefix="/v1/orgs", tags=["orgs"], route_class=GuardedAPIRoute)

SettingsDep = Annotated[Settings, Depends(get_settings)]
SenderDep = Annotated[EmailSender, Depends(get_email_sender)]


class RegisterPayload(BaseModel):
    # `extra="forbid"` is a security control, not tidiness: without it a client could
    # post `org_id`, `role` or `status` and a future handler that widened its model would
    # silently accept them. Mass assignment is the classic way a registration form
    # becomes a privilege-escalation endpoint.
    model_config = {"extra": "forbid"}

    full_name: str = Field(min_length=1, max_length=255)
    work_email: EmailStr
    company_name: str = Field(min_length=1, max_length=255)
    company_domain: str = Field(min_length=3, max_length=255)
    job_title: str = Field(min_length=1, max_length=128)
    org_size: str = Field(min_length=1, max_length=16)


class RegistrationAccepted(BaseModel):
    """Deliberately says nothing about what happened.

    No organisation id, no JUTSU ID, no "already exists". The next step is in the
    recipient's inbox, which is the only channel that proves they control the address.
    """

    status: str = "check_your_email"


@router.post("/register", status_code=status.HTTP_202_ACCEPTED)
@public("Registration creates the first account; requiring a session would be circular.")
async def register(
    payload: RegisterPayload,
    session: Db,
    settings: SettingsDep,
    sender: SenderDep,
) -> RegistrationAccepted:
    await register_organisation(
        session,
        RegistrationRequest(
            full_name=payload.full_name,
            work_email=str(payload.work_email),
            company_name=payload.company_name,
            company_domain=payload.company_domain,
            job_title=payload.job_title,
            org_size=payload.org_size,
        ),
        settings=settings,
        sender=sender,
    )
    # The outcome is deliberately discarded rather than returned: whether an organisation
    # was created is exactly the fact this endpoint must not disclose.
    return RegistrationAccepted()


class MemberCounts(BaseModel):
    """Headline numbers for the overview.

    Computed under the tenant scope, so these are this organisation's rows and nobody
    else's — row-level security does the filtering, not a WHERE clause someone has to
    remember to write.
    """

    total: int
    active: int
    invited: int
    deactivated: int
    admins: int


class OrganisationProfile(BaseModel):
    id: str
    name: str
    domain: str | None
    size_band: str | None
    status: str
    created_at: datetime
    members: MemberCounts


@router.get("/current")
@requires(Permission.ORG_READ)
async def read_current_organisation(
    principal: CurrentPrincipal, session: Db
) -> OrganisationProfile:
    """The signed-in caller's own organisation.

    There is deliberately no `{org_id}` variant. An organisation id in a path is an
    authorisation input from the client, and the whole architecture refuses those — the
    tenant comes from the session, server-side, every time. That also means this route
    cannot be used to probe whether another organisation exists.
    """
    org = (
        await session.execute(
            text("SELECT id, name, domain, size_band, status, created_at FROM orgs WHERE id = :id"),
            {"id": principal.org_id},
        )
    ).first()
    if org is None:
        # RLS returned nothing, which for a session-derived id means the organisation was
        # deleted underneath a live session rather than that the caller guessed wrong.
        raise NotFound("Organisation not found.")

    counts = (
        await session.execute(
            text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE status = 'active') AS active, "
                "count(*) FILTER (WHERE status = 'invited') AS invited, "
                "count(*) FILTER (WHERE status = 'deactivated') AS deactivated "
                "FROM users"
            )
        )
    ).one()

    # "Admin" is a capability, not a role name: anyone who may invite or assign roles is
    # one. Deriving it from the seeded matrix means adding a role never leaves this count
    # quietly wrong.
    admins = (
        await session.execute(
            text(
                "SELECT count(DISTINCT ur.user_id) FROM user_roles ur "
                "JOIN role_permissions rp ON rp.role_key = ur.role_key "
                "WHERE rp.permission_key = :permission"
            ),
            {"permission": Permission.MEMBER_INVITE.value},
        )
    ).scalar_one()

    return OrganisationProfile(
        id=str(org.id),
        name=org.name,
        domain=org.domain,
        size_band=org.size_band,
        status=org.status,
        created_at=org.created_at,
        members=MemberCounts(
            total=counts.total,
            active=counts.active,
            invited=counts.invited,
            deactivated=counts.deactivated,
            admins=admins,
        ),
    )
