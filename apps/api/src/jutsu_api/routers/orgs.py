"""Organisation registration — the entry point for a pilot.

Public, because there is nobody to authenticate yet: this route is what brings the first
account into existence.

The response is identical whether or not the domain was already registered. That is not
politeness, it is the difference between a sign-up form and a customer-enumeration
endpoint — without it, anyone could probe domains to learn which companies use JUTSU.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from jutsu_core.errors import NotFound
from jutsu_core.rbac import Permission, role_label
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text

from jutsu_api.auth_service import ChallengePurpose, open_session, verify_challenge
from jutsu_api.config import OTP_DIGITS, Settings, get_settings
from jutsu_api.deps import CurrentPrincipal, Db, get_email_sender
from jutsu_api.email import EmailSender, send_best_effort
from jutsu_api.emails import organisation_welcome
from jutsu_api.operations import org_overview, rename_organisation
from jutsu_api.registration import (
    RegistrationRequest,
    complete_registration,
    stage_registration,
)
from jutsu_api.routers.auth import set_session_cookies
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

    #: Optional, and genuinely so — a caller may omit them entirely.
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    industry: (
        Literal[
            "consulting",
            "technology",
            "finance",
            "healthcare",
            "manufacturing",
            "government",
            "other",
        ]
        | None
    ) = None

    #: `Literal[True]`, so an unticked box is a 422 rather than a silently stored `false`.
    #: There is no version field on purpose: the documents and their versions are server
    #: constants, because a client that names what it agreed to can name anything.
    terms_accepted: Literal[True]


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
    """Stage the registration and send a code. Creates no organisation.

    A mismatch between the work address and the claimed domain is answered plainly — both
    values came from this request, so saying so discloses nothing — but whether the domain
    is already registered is not, and cannot be reached from here at all: that answer is
    only produced after someone proves a mailbox at it.
    """
    await stage_registration(
        session,
        RegistrationRequest(
            full_name=payload.full_name,
            work_email=str(payload.work_email),
            company_name=payload.company_name,
            company_domain=payload.company_domain,
            job_title=payload.job_title,
            org_size=payload.org_size,
            country=payload.country,
            industry=payload.industry,
        ),
        settings=settings,
        sender=sender,
    )
    # The pending registration is deliberately discarded rather than returned: the token
    # belongs in the inbox and nowhere else.
    return RegistrationAccepted()


class RegisterVerifyPayload(BaseModel):
    model_config = {"extra": "forbid"}

    token: str = Field(min_length=16, max_length=128)
    code: str = Field(min_length=OTP_DIGITS, max_length=OTP_DIGITS)


class RegistrationComplete(BaseModel):
    #: Chosen by the server, never by the client, for the same reason `/v1/auth/verify`
    #: does it: a `next` parameter honoured here would be an open redirect with a fresh
    #: session attached.
    destination: str


@router.post("/register/verify")
@public("Completing a registration is what brings the first session into existence.")
async def register_verify(
    payload: RegisterVerifyPayload,
    response: Response,
    session: Db,
    settings: SettingsDep,
    sender: SenderDep,
) -> RegistrationComplete:
    """Redeem a registration code and create the organisation.

    Separate from `/v1/auth/verify` deliberately. That route documents "the same error for
    every rejection" and folds "valid code, but no membership" into it — and a brand-new
    registrant is exactly that case, so merging the two would either break its uniformity
    or make registration unreachable. Keeping them apart lets each stay absolute:
    `expected_purpose` means a sign-in code cannot complete a registration here, and a
    registration code cannot open a session there.
    """
    redeemed = await verify_challenge(
        session,
        token=payload.token,
        code=payload.code,
        expected_purpose=ChallengePurpose.REGISTER,
    )

    outcome = await complete_registration(
        session,
        token=payload.token,
        identity_id=redeemed.identity_id,
        challenge_id=redeemed.challenge_id,
        settings=settings,
    )
    # Every field the success path populates, asserted together. Not belt-and-braces:
    # `RegistrationOutcome` types them optional so the dataclass can also describe an
    # outcome that created nothing, and `str(None)` renders as the word "None" — which
    # would reach a customer as an organisation called None rather than as an error.
    assert outcome.org_id is not None and outcome.user_id is not None  # noqa: S101
    assert outcome.jutsu_id is not None and outcome.owner_role is not None  # noqa: S101
    assert outcome.org_name is not None and outcome.org_domain is not None  # noqa: S101
    assert outcome.owner_email is not None  # noqa: S101

    credentials = await open_session(
        session,
        identity_id=redeemed.identity_id,
        user_id=outcome.user_id,
        org_id=outcome.org_id,
    )
    set_session_cookies(
        response,
        token=credentials.token,
        csrf_token=credentials.csrf_token,
        settings=settings,
    )

    # The one message that carries the organisation identifier, sent at the one moment
    # there is an organisation to identify — and only to the address that just proved a
    # mailbox and redeemed this registration. No sign-in ever reproduces it.
    #
    # Best-effort on purpose. This runs inside the request transaction that created the
    # tenant, so raising would roll back an organisation, an owner, a role and a spent
    # JUTSU ID over a refused SMTP connection — and the registrant could not retry,
    # because the challenge and the staged payload are both consumed by now. They hold a
    # session either way; what a failure costs them is the copy of their identifiers,
    # which the console also shows.
    await send_best_effort(
        sender,
        organisation_welcome(
            to=outcome.owner_email,
            company_name=outcome.org_name,
            company_domain=outcome.org_domain,
            org_id=str(outcome.org_id),
            jutsu_id=outcome.jutsu_id,
            role=role_label(outcome.owner_role),
            app_url=settings.app_url,
        ),
    )

    return RegistrationComplete(destination="/admin")


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


class OrgRenamePayload(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=255)


class OrgRenamed(BaseModel):
    name: str


class OverviewOut(BaseModel):
    """The dashboard's operational figures. Every field is a count over a real table."""

    documents: int
    sources: int
    jobs_pending: int
    jobs_running: int
    jobs_failed_24h: int
    jobs_dead_letter: int
    invitations_pending: int
    audit_events_24h: int


@router.patch("/current")
@requires(Permission.ORG_UPDATE)
async def update_current_organisation(
    payload: OrgRenamePayload, principal: CurrentPrincipal, session: Db
) -> OrgRenamed:
    """Rename the organisation.

    The one mutable field. The domain anchors registration's one-org-per-domain rule and
    the verification trust chain, so it is deliberately not editable here — and there is
    no `{org_id}` variant for the same reason there is none on GET.
    """
    name = await rename_organisation(
        session, org_id=principal.org_id, actor_user_id=principal.user_id, name=payload.name
    )
    return OrgRenamed(name=name)


@router.get("/current/overview")
@requires(Permission.ORG_READ)
async def read_overview(principal: CurrentPrincipal, session: Db) -> OverviewOut:
    """Operational counts for the admin dashboard.

    Separate from `/current` so the identity card does not pay for eight aggregate
    subqueries on every load, and so a future cache can hold them for different times.
    """
    overview = await org_overview(session)
    return OverviewOut(**asdict(overview))
