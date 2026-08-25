"""The signed-in caller's own view.

`GET /v1/me` is what lets the frontend decide what to *render*. It is never what decides
what to *allow* — every permission listed here is re-checked server-side on the call it
gates. Hiding a button the caller cannot use is a courtesy; the guard on the endpoint
behind it is the control.

Note what is absent: no email, no display name, no organisation name. Those need a read
under the tenant scope and belong to the organisation endpoint. This one answers exactly
"who am I and what may I do", which is what the shell needs before it can render at all.
"""

from __future__ import annotations

from fastapi import APIRouter
from jutsu_core.rbac import Permission, Role
from pydantic import BaseModel
from sqlalchemy import text

from jutsu_api.deps import CurrentPrincipal, Db
from jutsu_api.security import GuardedAPIRoute, requires

router = APIRouter(prefix="/v1/me", tags=["me"], route_class=GuardedAPIRoute)


class Capabilities(BaseModel):
    """The caller's own identity and permission set.

    `org_id` and `user_id` are opaque UUIDs. They are safe to return to the person they
    describe, and they are what the UI uses for its own routing — but they are never
    accepted back as an authorisation input, which is why no endpoint takes an org id
    from the client.
    """

    org_id: str
    user_id: str
    jutsu_id: str | None
    # Typed as the enums, not as plain strings. Pydantic then emits them as enumerations
    # in the OpenAPI document, so the generated TypeScript is a real union rather than
    # `string` — which is the difference between §4.13 buying something and being
    # ceremony. A permission removed from the catalogue becomes a frontend build error
    # instead of a section that silently stops rendering.
    role: Role
    permissions: list[Permission]


@router.get("")
@requires(Permission.PROFILE_SELF_READ)
async def read_me(principal: CurrentPrincipal, session: Db) -> Capabilities:
    """Requires `profile:self_read`, which every role holds including a bare Member.

    Deliberately the most permissive gate in the catalogue, and the docstring here used
    to claim the opposite — that it required `org:read` and refused a Member. It does
    not, and it must not: this endpoint is how a client learns which surface it is
    allowed to render, so gating it on an admin permission would leave a Member unable
    to discover that they are a Member.
    """
    jutsu_id = (
        await session.execute(
            text("SELECT jutsu_id FROM users WHERE id = :u"), {"u": principal.user_id}
        )
    ).scalar_one_or_none()

    return Capabilities(
        org_id=str(principal.org_id),
        user_id=str(principal.user_id),
        jutsu_id=jutsu_id,
        role=principal.role,
        permissions=sorted(principal.permissions),
    )
