"""Source identities: what a caller may see, and who may change it (ADR 0010).

Four routes with four different guards, and the differences are the design:

  GET    /v1/me/identities                        integration:self_manage — everyone
  GET    /v1/employees/{id}/identities            integration:read
  POST   /v1/employees/{id}/identities            integration:connect
  DELETE /v1/employees/{id}/identities/{sid}      integration:revoke

The self route holds the most permissive permission in the catalogue, deliberately: §31
says a person should be able to see what JUTSU has connected on their behalf, and a
transparency surface that an ordinary member cannot open is not a transparency surface.

The organisation is never a parameter on any of them. It comes from the session, and
row-level security scopes every read — so a user id belonging to another tenant is simply
absent rather than refused, and the caller learns nothing from the difference.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, status
from jutsu_core import SourceSystem
from jutsu_core.rbac import Permission
from pydantic import BaseModel, Field

from jutsu_api.deps import CurrentPrincipal, Db
from jutsu_api.identities import (
    LinkedIdentity,
    link_identity,
    list_identities,
    revoke_identity,
)
from jutsu_api.security import GuardedAPIRoute, requires

router = APIRouter(prefix="/v1", tags=["identities"], route_class=GuardedAPIRoute)


class SourceIdentityView(BaseModel):
    """One linked identity, as a caller sees it.

    `subject` is returned. It is an identifier rather than a secret — the point of the
    transparency surface is to say *which* account is linked, and an opaque row that will
    not name it answers nothing. It is never a credential; no token is stored anywhere in
    this product.
    """

    id: str
    #: Typed as the enum so OpenAPI emits a union and the generated TypeScript is a real
    #: literal type rather than `string` (§4.13).
    source_system: SourceSystem
    subject: str
    is_active: bool
    linked_at: datetime
    revoked_at: datetime | None
    #: How the link was made — `verified_email` or `admin`. Who made it is in the audit
    #: log; this is a property of the row, not of the event.
    linked_by: str


class SourceIdentityPage(BaseModel):
    items: list[SourceIdentityView]


class LinkPayload(BaseModel):
    # `extra="forbid"` is a security control here, not tidiness. Without it a client could
    # post `user_id`, `org_id` or `is_active` and any future widening of this model would
    # silently accept them — which on an endpoint that grants document access is the
    # difference between an admin tool and a privilege-escalation endpoint.
    model_config = {"extra": "forbid"}

    source_system: SourceSystem
    #: The provider-native immutable subject. Never an email address, except for
    #: `SourceSystem.LOCAL` where the address genuinely is the subject the source issues.
    subject: str = Field(min_length=1, max_length=255)


def _view(identity: LinkedIdentity) -> SourceIdentityView:
    return SourceIdentityView(
        id=str(identity.id),
        source_system=identity.source_system,
        subject=identity.subject,
        is_active=identity.is_active,
        linked_at=identity.linked_at,
        revoked_at=identity.revoked_at,
        linked_by=identity.linked_by,
    )


@router.get("/me/identities")
@requires(Permission.INTEGRATION_SELF_MANAGE)
async def read_my_identities(principal: CurrentPrincipal, session: Db) -> SourceIdentityPage:
    """Which accounts are linked to the caller, and which were revoked.

    Held by every role including a bare Member. Seeing what has been connected on your
    behalf is not an administrative privilege, and gating it on one would leave the people
    with the least power least able to check.
    """
    identities = await list_identities(session, user_id=principal.user_id)
    return SourceIdentityPage(items=[_view(identity) for identity in identities])


@router.get("/employees/{user_id}/identities")
@requires(Permission.INTEGRATION_READ)
async def read_employee_identities(
    user_id: UUID, principal: CurrentPrincipal, session: Db
) -> SourceIdentityPage:
    """Which accounts are linked to one employee.

    Read-only, so there is no rank check: `integration:read` is the ceiling, and an
    administrator who may see the organisation's integrations may see which of its people
    are attached to them.
    """
    identities = await list_identities(session, user_id=user_id)
    return SourceIdentityPage(items=[_view(identity) for identity in identities])


@router.post("/employees/{user_id}/identities", status_code=status.HTTP_201_CREATED)
@requires(Permission.INTEGRATION_CONNECT)
async def create_employee_identity(
    user_id: UUID, payload: LinkPayload, principal: CurrentPrincipal, session: Db
) -> SourceIdentityView:
    """Link a provider subject to an employee. **This grants document access.**

    Everything about this endpoint follows from that sentence. The permission is the
    outer gate; inside, `link_identity` refuses a self-link outright, enforces the rank
    ceiling, resolves the target under row-level security, and writes an audit row naming
    the actor.

    The self-link refusal is not a permission check and cannot be granted away. §17 keeps
    roles and ACLs apart — nothing in `Permission` may confer a document read — and an
    administrator linking themselves would be exactly that.
    """
    identity = await link_identity(
        session,
        actor_id=principal.user_id,
        actor_org_id=principal.org_id,
        actor_role=principal.role,
        target_user_id=user_id,
        source_system=payload.source_system,
        subject=payload.subject,
    )
    return _view(identity)


@router.delete(
    "/employees/{user_id}/identities/{identity_id}", status_code=status.HTTP_204_NO_CONTENT
)
@requires(Permission.INTEGRATION_REVOKE)
async def delete_employee_identity(
    user_id: UUID, identity_id: UUID, principal: CurrentPrincipal, session: Db
) -> None:
    """Revoke a linked identity. Effective on the target's next request.

    A deactivation rather than a delete, so the row survives for the audit trail. There is
    nothing to invalidate: principals are resolved fresh on every request, which is what
    makes "immediately" true rather than aspirational.
    """
    await revoke_identity(
        session,
        actor_id=principal.user_id,
        actor_org_id=principal.org_id,
        actor_role=principal.role,
        target_user_id=user_id,
        identity_id=identity_id,
    )
