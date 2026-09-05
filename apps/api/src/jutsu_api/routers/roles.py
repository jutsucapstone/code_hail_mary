"""The role taxonomy over HTTP: one catalogue, one assignment.

  GET   /v1/role-catalogue                       profile:self_read — everybody
  GET   /v1/employees/{id}/role-assignment       member:read
  PATCH /v1/employees/{id}/role-assignment       member:assign_role_code (+ rank rules)

The catalogue is readable by every authenticated caller, gated on the permission every
role already holds. It is global reference data with no tenant in it, and an employee who
cannot read it cannot be shown the name of their own seniority — the page would have the
key `senior_consultant` and nothing to render.

Assignment is deliberately NOT part of `PATCH /v1/me/profile`. That endpoint is
self-service under `profile:self_update`, which every role holds; putting the taxonomy
there would let anybody promote themselves to Partner. The split — self-editable business
profile, admin-assigned standing — is the whole reason this is a separate route with a
separate guard.
"""

from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter
from jutsu_core.errors import NotFound
from jutsu_core.rbac import Permission
from pydantic import BaseModel, Field

from jutsu_api.deps import CurrentPrincipal, Db
from jutsu_api.roles import RoleAssignment, assign_role_taxonomy, read_catalogue, read_taxonomy
from jutsu_api.security import GuardedAPIRoute, requires

router = APIRouter(prefix="/v1", tags=["roles"], route_class=GuardedAPIRoute)


class Discipline(BaseModel):
    key: str
    display_name: str


class Practice(BaseModel):
    key: str
    display_name: str
    disciplines: list[Discipline]


class Level(BaseModel):
    key: str
    display_name: str
    #: Non-unique on purpose: "Analyst / BTA" and "Senior Manager / Specialist Leader"
    #: are peers in the source, so equal ranks are genuine peers rather than a bug.
    rank: int
    description: str
    #: The code this level suggests, or null where the catalogue has no equivalent
    #: (Director). A suggestion for the admin UI, never applied automatically.
    suggested_code: str | None


class Title(BaseModel):
    key: str
    practice_key: str
    discipline_key: str | None
    display_name: str
    #: More than one entry means the source document is genuinely ambiguous about this
    #: title's seniority; the assignment must pick one of them.
    level_keys: list[str]
    default_level_key: str


class Code(BaseModel):
    code: str
    display_name: str
    tier: int
    category: str
    description: str
    #: A governance seat. The UI uses this to mark the code; the API enforces the extra
    #: rank rule regardless of what the UI does with it.
    privileged: bool


class Catalogue(BaseModel):
    practices: list[Practice]
    levels: list[Level]
    titles: list[Title]
    codes: list[Code]


class Taxonomy(BaseModel):
    """One person's assignment, with the catalogue's names already resolved."""

    practice_key: str | None
    practice: str | None
    discipline: str | None
    role_title_key: str | None
    role_title: str | None
    role_level_key: str | None
    role_level: str | None
    role_level_rank: int | None
    role_code: str | None
    role_code_name: str | None
    role_code_tier: int | None
    mapping_status: str


class AssignmentPayload(BaseModel):
    """What an administrator is setting.

    `extra="forbid"` is load-bearing here beyond tidiness: it is what stops a client
    smuggling `org_id`, `user_id` or `role_mapping_status` into the write. The tenant and
    the target come from the session and the path, and the status is derived from what
    was actually assigned.

    Every field is optional and `None` means *clear it* — the same PATCH semantics the
    profile endpoint uses. Sending `{}` is a no-op rather than a wipe.
    """

    model_config = {"extra": "forbid"}

    practice_key: str | None = Field(default=None, max_length=48)
    role_title_key: str | None = Field(default=None, max_length=64)
    role_title_custom: str | None = Field(default=None, max_length=128)
    role_level_key: str | None = Field(default=None, max_length=48)
    role_code: str | None = Field(default=None, max_length=3)


@router.get("/role-catalogue")
@requires(Permission.PROFILE_SELF_READ)
async def read_role_catalogue(principal: CurrentPrincipal, session: Db) -> Catalogue:
    """The practices, levels, titles and platform codes this deployment knows.

    Identical for every organisation — it is migration-seeded reference data, not tenant
    data — so there is nothing here to scope and nothing a caller could widen.
    """
    catalogue = await read_catalogue(session)
    return Catalogue(
        practices=[
            Practice(
                key=practice.key,
                display_name=practice.display_name,
                disciplines=[
                    Discipline(key=key, display_name=name) for key, name in practice.disciplines
                ],
            )
            for practice in catalogue.practices
        ],
        levels=[Level(**asdict(level)) for level in catalogue.levels],
        titles=[
            Title(
                key=title.key,
                practice_key=title.practice_key,
                discipline_key=title.discipline_key,
                display_name=title.display_name,
                level_keys=list(title.level_keys),
                default_level_key=title.default_level_key,
            )
            for title in catalogue.titles
        ],
        codes=[Code(**asdict(code)) for code in catalogue.codes],
    )


@router.get("/employees/{user_id}/role-assignment")
@requires(Permission.MEMBER_READ)
async def read_employee_taxonomy(
    user_id: UUID, principal: CurrentPrincipal, session: Db
) -> Taxonomy:
    """One employee's assignment.

    Row-level security scopes the read, so a user id from another tenant is not found
    rather than refused — the two are indistinguishable to the caller, which is the point.
    """
    view = await read_taxonomy(session, user_id=user_id)
    if view is None:
        raise NotFound("That employee has no role assignment yet.")
    return Taxonomy(**asdict(view))


@router.patch("/employees/{user_id}/role-assignment")
@requires(Permission.MEMBER_ASSIGN_ROLE_CODE)
async def assign_employee_taxonomy(
    user_id: UUID,
    payload: AssignmentPayload,
    principal: CurrentPrincipal,
    session: Db,
) -> Taxonomy:
    """Set an employee's practice, title, normalized level and platform role code.

    The permission is the first gate and the service adds the second: seating somebody in
    one of the four governance codes needs Owner or Super Admin, and may never be done to
    oneself. Every change writes an audit row carrying the before and after of each field
    that actually moved.
    """
    assignment = RoleAssignment(
        values=payload.model_dump(),
        provided=frozenset(payload.model_fields_set),
    )
    view = await assign_role_taxonomy(
        session,
        actor_user_id=principal.user_id,
        actor_role=principal.role,
        target_user_id=user_id,
        org_id=principal.org_id,
        assignment=assignment,
    )
    return Taxonomy(**asdict(view))
