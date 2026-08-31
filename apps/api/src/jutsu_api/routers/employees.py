"""People in an organisation: listing them, inviting them, joining, and role changes.

Five routes with different guards, and the differences are the design:

  GET   /v1/employees                  requires member:read
  POST  /v1/employees/invitations      requires member:invite
  GET   /v1/invitations                requires member:invite — who may send them may
                                       see what happened to them
  PATCH /v1/employees/{id}/role        requires member:assign_role, plus the rank rules
                                       the service enforces
  POST  /v1/invitations/accept         public — the invitee has no session yet; the
                                       invitation token is what proves who they are
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from jutsu_core.rbac import Permission, Role, role_label
from pydantic import BaseModel, EmailStr, Field

from jutsu_api.auth_service import open_session
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import CurrentPrincipal, Db, get_email_sender
from jutsu_api.email import EmailSender, send_best_effort
from jutsu_api.emails import employee_welcome
from jutsu_api.invitations import accept_invitation, invite_employee, list_employees
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
    role: Role | None
    created_at: datetime
    last_activity_at: datetime | None


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
) -> EmployeePage:
    """Everyone in the caller's organisation.

    The organisation is never a parameter. It comes from the session, and row-level
    security scopes the query — so there is no combination of arguments that returns
    another tenant's people.
    """
    rows, next_cursor = await list_employees(session, limit=limit, cursor=cursor, query=q)
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
    )
    return InvitationAccepted()


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
