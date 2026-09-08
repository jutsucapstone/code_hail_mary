"""People in an organisation: listing them, inviting them, joining, and role changes.

Seven routes with different guards, and the differences are the design:

  GET   /v1/employees                          requires member:read
  POST  /v1/employees/invitations              requires member:invite
  POST  /v1/employees/invitations/preview      requires member:invite — writes nothing
  POST  /v1/employees/invitations/bulk         requires member:invite
  GET   /v1/invitations                        requires member:invite — who may send them
                                               may see what happened to them
  POST  /v1/invitations/{id}/revoke            requires member:invite
  POST  /v1/invitations/{id}/resend            requires member:invite, and re-checks the
                                               rank ceiling against whoever pressed it
  PATCH /v1/employees/{id}/role                requires member:assign_role, plus the rank
                                               rules the service enforces
  POST  /v1/invitations/accept                 public — the invitee has no session yet;
                                               the invitation token is what proves who
                                               they are

The two bulk routes take the *same* permission as the single one, deliberately. Inviting
eighty people is inviting one person eighty times; a separate, higher permission would
either lock administrators out of a tool they are already entitled to use or become a
reason to hand out a broader role.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from jutsu_core.errors import ValidationFailed
from jutsu_core.rbac import Permission, Role, role_label
from pydantic import BaseModel, EmailStr, Field, model_validator

from jutsu_api.auth_service import open_session
from jutsu_api.bulk_invitations import (
    MAX_BULK_ROWS,
    ROLE_TITLE_MAX,
    BulkOutcome,
    BulkRow,
    ClassifiedRow,
    classify,
    invite_many,
    parse_csv,
    parse_pasted,
    parse_xlsx,
)
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import CurrentPrincipal, Db, get_email_sender
from jutsu_api.email import EmailSender, send_best_effort
from jutsu_api.emails import employee_welcome
from jutsu_api.invitations import (
    accept_invitation,
    invite_employee,
    list_employees,
    resend_invitation,
    revoke_invitation,
)
from jutsu_api.operations import change_member_role, list_invitations
from jutsu_api.routers.auth import set_session_cookies
from jutsu_api.security import GuardedAPIRoute, destination_for, public, requires

router = APIRouter(prefix="/v1", tags=["employees"], route_class=GuardedAPIRoute)

SettingsDep = Annotated[Settings, Depends(get_settings)]
SenderDep = Annotated[EmailSender, Depends(get_email_sender)]


class Employee(BaseModel):
    id: str
    email: str
    display_name: str | None
    jutsu_id: str | None
    status: str
    #: The RBAC role — what this person may DO. Not to be confused with `role_code`
    #: below, which is where they sit on the org chart and confers nothing.
    role: Role | None
    created_at: datetime
    last_activity_at: datetime | None

    #: The assigned taxonomy, flattened for a table. `role_title` is the person's ACTUAL
    #: title in their practice's vocabulary and `role_level` the normalized seniority
    #: that makes those titles comparable — both are shown, because a roster that
    #: replaced "Audit Senior" with "Senior Consultant" would be lying to the reader
    #: about what the person is called.
    practice_key: str | None = None
    practice: str | None = None
    role_title: str | None = None
    role_level_key: str | None = None
    role_level: str | None = None
    role_level_rank: int | None = None
    role_code: str | None = None
    mapping_status: str = "unmapped"


class EmployeePage(BaseModel):
    """A page of people, plus the cursor for the next one.

    `next_cursor` is opaque and keyset-based rather than a page number. Offset paging over
    a table being written to skips and duplicates rows between pages — and this table is
    written to exactly when an admin is looking at it, because that is when they invite.
    """

    items: list[Employee]
    next_cursor: str | None


class InvitePayload(BaseModel):
    model_config = {"extra": "forbid"}

    email: EmailStr
    #: A role, chosen from the catalogue. `extra="forbid"` above plus this enum means a
    #: client cannot smuggle an arbitrary string into `user_roles.role_key`.
    role: Role
    #: Optional free-text TITLE (§1's "other option where user can write"). Becomes the
    #: invitee's profile designation — vocabulary, never authority: permissions come
    #: from `role` above and nothing else.
    role_title: str | None = Field(default=None, max_length=128)


class InvitationAccepted(BaseModel):
    status: str = "sent"


class AcceptPayload(BaseModel):
    model_config = {"extra": "forbid"}

    token: str = Field(min_length=16, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)


class AcceptResult(BaseModel):
    #: Shown once, on the screen that follows. It is the identifier the person will be
    #: asked for when they sign in again.
    jutsu_id: str
    destination: str


@router.get("/employees")
@requires(Permission.MEMBER_READ)
async def read_employees(
    principal: CurrentPrincipal,
    session: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    q: Annotated[str | None, Query(max_length=128)] = None,
    practice: Annotated[str | None, Query(max_length=48)] = None,
    level: Annotated[str | None, Query(max_length=48)] = None,
    role_code: Annotated[str | None, Query(max_length=3)] = None,
    unmapped: Annotated[bool, Query()] = False,
) -> EmployeePage:
    """Everyone in the caller's organisation.

    The organisation is never a parameter. It comes from the session, and row-level
    security scopes the query — so there is no combination of arguments that returns
    another tenant's people.

    `level` is the Expert Finder filter: it matches the NORMALIZED seniority, so asking
    for `senior_consultant` returns the Senior Software Engineer, the Audit Senior and
    the Senior Tax Consultant together even though no two of those titles share a word.
    `unmapped` is the other side of the same coin — the review queue of people nobody has
    placed in the taxonomy yet, which migration 0018 deliberately left for an
    administrator rather than guessing.
    """
    rows, next_cursor = await list_employees(
        session,
        limit=limit,
        cursor=cursor,
        query=q,
        practice=practice,
        level=level,
        code=role_code,
        unmapped=unmapped,
    )
    return EmployeePage(
        items=[Employee.model_validate(row) for row in rows], next_cursor=next_cursor
    )


@router.post("/employees/invitations", status_code=status.HTTP_202_ACCEPTED)
@requires(Permission.MEMBER_INVITE)
async def create_invitation(
    payload: InvitePayload,
    principal: CurrentPrincipal,
    session: Db,
    settings: SettingsDep,
    sender: SenderDep,
) -> InvitationAccepted:
    await invite_employee(
        session,
        actor=principal,
        email=str(payload.email),
        role=payload.role,
        settings=settings,
        sender=sender,
        role_title=payload.role_title,
    )
    return InvitationAccepted()


#: Bounds on the pasted and uploaded forms, in characters of the request body.
#:
#: `MAX_BULK_ROWS` is the bound that matters, but it is only knowable after parsing — and
#: a parser should never be handed an unbounded string. Sixty-four thousand characters is
#: roughly two thousand addresses: far past the row limit, so an over-long paste is
#: refused for the reason the administrator can act on ("too many addresses") rather than
#: for its byte count.
_MAX_PASTED_CHARS = 64_000
_MAX_CSV_CHARS = 1_000_000
#: base64 inflates by four thirds, and `MAX_XLSX_BYTES` is a megabyte of workbook.
_MAX_XLSX_CHARS = 1_400_000


class BulkSource(BaseModel):
    """What the administrator supplied, in whichever of the three forms they had it.

    Exactly one of `emails`, `csv` or `xlsx_base64`. A payload carrying two is refused
    rather than silently preferring one, because the two would disagree about the roles
    and the administrator would never see which had been used.
    """

    model_config = {"extra": "forbid"}

    #: Addresses pasted as text — newline, comma or semicolon separated, and
    #: `Name <address>` accepted because that is what a mail client copies.
    emails: str | None = Field(default=None, max_length=_MAX_PASTED_CHARS)
    #: The text of a CSV or TSV file, read by the browser. No multipart upload: the file
    #: is text, `File.text()` already has it, and a JSON body keeps the generated
    #: TypeScript client honest.
    csv: str | None = Field(default=None, max_length=_MAX_CSV_CHARS)
    #: A `.xlsx` workbook, base64-encoded. A zip of XML cannot be read as text, so this
    #: is the one input that arrives as bytes; the parser bounds it before opening it.
    xlsx_base64: str | None = Field(default=None, max_length=_MAX_XLSX_CHARS)
    #: The role for rows that do not name one. Never widens what the actor may grant —
    #: `classify` re-checks every row against the actor's own rank.
    role: Role = Role.MEMBER

    @model_validator(mode="after")
    def exactly_one_source(self) -> BulkSource:
        supplied = [value for value in (self.emails, self.csv, self.xlsx_base64) if value]
        if len(supplied) != 1:
            raise ValueError("Supply exactly one of emails, csv or xlsx_base64.")
        return self


class BulkInviteRow(BaseModel):
    """One row of the reviewed preview, sent back to be acted on.

    The send takes explicit rows rather than the original paste, because the point of the
    preview is that the administrator edits it: fixes a typo, changes somebody's role,
    removes the four people who left. Re-parsing the paste would discard all of that.
    """

    model_config = {"extra": "forbid"}

    #: **A bounded string, deliberately NOT `EmailStr`.**
    #:
    #: The preview marks a row `ready` using `bulk_invitations._EMAIL`, which is
    #: deliberately permissive because the authority on an address is the mailbox that
    #: answers it. `EmailStr` is stricter — so a row the preview promised to invite could
    #: fail validation here, and because Pydantic validates the whole body, ONE such
    #: address rejected the entire batch with a 422 before `invite_many` ever ran. The
    #: browser was faithfully posting back what the preview had approved.
    #:
    #: The send re-runs `classify` regardless, so a genuinely unusable address comes back
    #: as that one row marked `invalid_email` while everybody else is invited — which is
    #: the whole point of a bulk import.
    email: str = Field(min_length=3, max_length=320)
    role: Role
    #: The parsers truncate to this same length, so the preview can never hand back a
    #: title the send would refuse. `invite_employee` truncates once more on the way to the
    #: database; all three agree on 128 on purpose.
    role_title: str | None = Field(default=None, max_length=ROLE_TITLE_MAX)


class BulkInvitePayload(BaseModel):
    model_config = {"extra": "forbid"}

    rows: list[BulkInviteRow] = Field(min_length=1, max_length=MAX_BULK_ROWS)


class BulkRowResult(BaseModel):
    """One row and what happened, or would happen, to it."""

    email: str
    role: Role
    role_title: str | None
    #: ready · sent · already_member · already_invited · duplicate · invalid_email ·
    #: invalid_role · role_too_high · failed
    outcome: BulkOutcome
    #: One sentence, written for the administrator reading the table. Never a token, and
    #: never anything about an address outside this organisation.
    detail: str


class BulkPreview(BaseModel):
    rows: list[BulkRowResult]
    #: Counted server-side so the button's label and the work it does cannot disagree.
    ready: int
    total: int


class BulkInviteOutcome(BaseModel):
    rows: list[BulkRowResult]
    sent: int
    failed: int


def _rendered(rows: list[ClassifiedRow]) -> list[BulkRowResult]:
    return [
        BulkRowResult(
            email=row.email,
            role=row.role,
            role_title=row.role_title,
            outcome=row.outcome,
            detail=row.detail,
        )
        for row in rows
    ]


@router.post("/employees/invitations/preview")
@requires(Permission.MEMBER_INVITE)
async def preview_invitations(
    payload: BulkSource, principal: CurrentPrincipal, session: Db
) -> BulkPreview:
    """What would happen to each address. Sends nothing and writes nothing.

    This is the whole reason bulk onboarding is two requests. An administrator pasting a
    list from last quarter's roster wants to see the six people who already have accounts
    and the two misspelt addresses *before* seventy-two others receive mail — and once
    those are shown, an invitation nobody can un-send is a decision rather than an
    accident.

    It is gated and tenant-scoped exactly like the send: the member and invitation lookups
    run under row-level security, so the answer for an address outside this organisation
    is always "will be invited", never "already a member somewhere else".
    """
    rows = _parse(payload)
    if len(rows) > MAX_BULK_ROWS:
        raise ValidationFailed(
            f"That is {len(rows)} addresses. Import up to {MAX_BULK_ROWS} at a time."
        )

    classified = await classify(session, actor=principal, rows=rows)
    return BulkPreview(
        rows=_rendered(classified),
        ready=sum(1 for row in classified if row.outcome is BulkOutcome.READY),
        total=len(classified),
    )


@router.post("/employees/invitations/bulk", status_code=status.HTTP_202_ACCEPTED)
@requires(Permission.MEMBER_INVITE)
async def create_invitations(
    payload: BulkInvitePayload,
    principal: CurrentPrincipal,
    session: Db,
    settings: SettingsDep,
    sender: SenderDep,
) -> BulkInviteOutcome:
    """Invite everyone the administrator approved, and report each row's fate.

    202 rather than 200: some rows may not have been invited, and the response body — not
    the status — is what says which. A 200 over a batch where eleven rows failed would be
    a lie the client has to unpick.

    Idempotent in the way that matters for a retry: re-sending the same rows returns
    `already_invited` for everyone who got one, so pressing the button twice does not
    email anybody twice.
    """
    result = await invite_many(
        session,
        actor=principal,
        rows=[
            BulkRow(email=str(row.email), role=row.role, role_title=row.role_title)
            for row in payload.rows
        ],
        settings=settings,
        sender=sender,
    )
    return BulkInviteOutcome(rows=_rendered(result.rows), sent=result.sent, failed=result.failed)


def _parse(payload: BulkSource) -> list[BulkRow]:
    if payload.emails:
        return parse_pasted(payload.emails, default_role=payload.role)
    if payload.csv:
        return parse_csv(payload.csv, default_role=payload.role)
    if payload.xlsx_base64:
        try:
            # `validate=True`, so a body carrying anything but base64 is refused here
            # rather than quietly decoding to bytes that are not the chosen file.
            workbook = base64.b64decode(payload.xlsx_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationFailed("That file could not be read.") from exc
        return parse_xlsx(workbook, default_role=payload.role)

    # Unreachable through the model validator, and still not an assertion: a refusal the
    # caller can read beats an `AssertionError` if that validator is ever relaxed.
    raise ValidationFailed("Supply addresses to invite.")


class InvitationEntry(BaseModel):
    id: UUID
    email: str
    role: Role
    #: Derived server-side: pending | accepted | revoked | expired.
    status: str
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None


class InvitationPage(BaseModel):
    items: list[InvitationEntry]
    next_cursor: str | None


class RoleChangePayload(BaseModel):
    model_config = {"extra": "forbid"}

    role: Role


class RoleChanged(BaseModel):
    user_id: str
    role: Role
    previous_role: Role


@router.get("/invitations")
@requires(Permission.MEMBER_INVITE)
async def read_invitations(
    principal: CurrentPrincipal,
    session: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
) -> InvitationPage:
    """What happened to every invitation this organisation sent.

    Gated on the same permission that sends them: an invitation is an email address, and
    who may create that exposure may see its state — nobody with less.
    """
    page = await list_invitations(session, limit=limit, cursor=cursor)
    return InvitationPage(
        items=[InvitationEntry(**asdict(row)) for row in page.items],
        next_cursor=page.next_cursor,
    )


class InvitationRevoked(BaseModel):
    #: Echoed so the page can say whose invitation it just cancelled without holding a
    #: stale copy of the row it optimistically removed.
    email: str


@router.post("/invitations/{invitation_id}/revoke")
@requires(Permission.MEMBER_INVITE)
async def revoke(
    invitation_id: UUID, principal: CurrentPrincipal, session: Db
) -> InvitationRevoked:
    """Cancel an invitation that is still waiting.

    Gated on the permission that sent it: whoever may create that exposure may withdraw
    it. An accepted invitation is a 404 here — that person is a member, and unmaking a
    membership is deactivation, not cancellation.
    """
    email = await revoke_invitation(session, actor=principal, invitation_id=invitation_id)
    return InvitationRevoked(email=email)


@router.post("/invitations/{invitation_id}/resend", status_code=status.HTTP_202_ACCEPTED)
@requires(Permission.MEMBER_INVITE)
async def resend(
    invitation_id: UUID,
    principal: CurrentPrincipal,
    session: Db,
    settings: SettingsDep,
    sender: SenderDep,
) -> InvitationAccepted:
    """Issue a fresh invitation to the same address, and kill the old one.

    The most-asked admin question is "they never got it". The answer is a new token, not
    the old one resent: reusing it would extend a live credential's life every time the
    button was pressed, and leave two working copies in two inboxes if the first message
    merely arrived late.
    """
    await resend_invitation(
        session,
        actor=principal,
        invitation_id=invitation_id,
        settings=settings,
        sender=sender,
    )
    return InvitationAccepted()


@router.patch("/employees/{user_id}/role")
@requires(Permission.MEMBER_ASSIGN_ROLE)
async def assign_role(
    user_id: UUID,
    payload: RoleChangePayload,
    principal: CurrentPrincipal,
    session: Db,
) -> RoleChanged:
    """Change a member's role, inside the escalation rules.

    The service refuses self-changes, refuses acting on a peer or superior, and refuses
    granting a role at or above the actor's own rank — which makes `owner` structurally
    unassignable here. Every successful change writes an audit row naming the actor and
    both roles.
    """
    previous = await change_member_role(
        session,
        actor_user_id=principal.user_id,
        actor_role=principal.role,
        target_user_id=user_id,
        new_role=payload.role,
        org_id=principal.org_id,
    )
    return RoleChanged(user_id=str(user_id), role=payload.role, previous_role=previous)


@router.post("/invitations/accept")
@public("The invitee has no session; the invitation token is what identifies them.")
async def accept(
    payload: AcceptPayload,
    response: Response,
    session: Db,
    settings: SettingsDep,
    sender: SenderDep,
) -> AcceptResult:
    """Join an organisation, and sign in.

    No second code is sent. The token reached the invited address and nowhere else, so
    holding it already proves the same thing an emailed code would — sending another
    would be ceremony, not security.

    A welcome does go out, and it is not ceremony. `jutsu_id` below is shown on exactly
    one screen, and the console asks for it by name at every subsequent sign-in — so a
    closed tab currently costs somebody their identifier and an email to their
    administrator. The message carries that, their role and the address to sign in with.
    It carries no organisation identifier: the sign-in form does not ask for one.
    """
    accepted = await accept_invitation(
        session, token=payload.token, full_name=payload.full_name, settings=settings
    )

    credentials = await open_session(
        session,
        identity_id=accepted.identity_id,
        user_id=accepted.user_id,
        org_id=accepted.org_id,
    )
    set_session_cookies(
        response,
        token=credentials.token,
        csrf_token=credentials.csrf_token,
        settings=settings,
    )

    # Best-effort, for the same reason registration's is: this runs inside the
    # transaction that created the account, and losing a welcome is a far smaller failure
    # than rolling back a person's membership over a mail provider having a bad minute.
    # The invitation is spent by now, so there would be nothing to retry with.
    await send_best_effort(
        sender,
        employee_welcome(
            to=accepted.email,
            organisation=accepted.org_name,
            jutsu_id=accepted.jutsu_id,
            role=role_label(accepted.role),
            app_url=settings.app_url,
        ),
    )

    # Chosen by the server. A destination from the request would be an open redirect with
    # a freshly minted session attached.
    return AcceptResult(jutsu_id=accepted.jutsu_id, destination=destination_for(accepted.role))


class DepartmentRow(BaseModel):
    #: The department string exactly as people typed it into their own profiles. Not an
    #: entity: "Platform" and "platform" are two rows here, and that is the honest
    #: rendering of self-service free text until departments become a managed table.
    name: str
    members: int


class DepartmentsOut(BaseModel):
    items: list[DepartmentRow]
    #: People whose profile has no department yet — shown so the totals add up rather
    #: than quietly excluding them.
    unassigned: int


@router.get("/departments")
@requires(Permission.MEMBER_READ)
async def read_departments(principal: CurrentPrincipal, session: Db) -> DepartmentsOut:
    """Departments as people have declared them, with member counts.

    An aggregation over `employee_profiles.department` — self-service free text, not a
    managed entity. The response says so via its shape: names arrive as typed, and the
    unassigned count is first-class. Making departments a real table (create, rename,
    assign, RLS) is its own migration when the organisation model needs it.
    """
    from sqlalchemy import text as _sql

    rows = (
        await session.execute(
            _sql(
                "SELECT ep.department AS name, count(*) AS members "
                "FROM employee_profiles ep "
                "WHERE ep.department IS NOT NULL AND ep.department != '' "
                "GROUP BY ep.department ORDER BY members DESC, name"
            )
        )
    ).all()
    unassigned = (
        await session.execute(
            _sql(
                "SELECT count(*) FROM users u LEFT JOIN employee_profiles ep "
                "ON ep.user_id = u.id "
                "WHERE u.status != 'deactivated' "
                "AND (ep.department IS NULL OR ep.department = '')"
            )
        )
    ).scalar_one()
    return DepartmentsOut(
        items=[DepartmentRow(name=row.name, members=row.members) for row in rows],
        unassigned=unassigned,
    )
