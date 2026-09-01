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

from datetime import date, datetime

from fastapi import APIRouter
from jutsu_core.rbac import Permission, Role
from jutsu_retrieval.search import ACL_PREDICATE
from pydantic import BaseModel, Field
from sqlalchemy import text

from jutsu_api.auth_service import scoped_acl_principals
from jutsu_api.deps import CurrentPrincipal, Db
from jutsu_api.profiles import EmployeeProfile, ProfileUpdate, read_profile, upsert_profile
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


class ProfileView(BaseModel):
    """The caller's own employee profile — exactly the columns the table has.

    No `user_id` and no `org_id`. They are identity rather than profile data, the caller
    already has both from `GET /v1/me`, and leaving them off this model means there is no
    field here that could ever be mistaken for an input.
    """

    employee_code: str | None
    department: str | None
    designation: str | None
    joining_date: date | None
    phone_e164: str | None
    skills: list[str]
    responsibilities: str | None
    updated_at: datetime


class ProfilePatch(BaseModel):
    """A partial update. Absent means "leave alone"; explicit `null` means "clear".

    `extra="forbid"` is a security control here, not tidiness — the same reasoning
    `LinkPayload` records. Without it a client could post `user_id` or `org_id`, and any
    future widening of this model would silently start accepting them. On an endpoint
    that writes a tenant-scoped row, that is the difference between a profile form and a
    way to write into somebody else's organisation.

    Lengths mirror the column definitions from migration 0002 exactly, so a value that
    would be truncated by the database is refused by validation with a message the caller
    can act on instead.
    """

    model_config = {"extra": "forbid"}

    employee_code: str | None = Field(default=None, max_length=64)
    department: str | None = Field(default=None, max_length=128)
    designation: str | None = Field(default=None, max_length=128)
    joining_date: date | None = None
    #: E.164: a leading `+`, a non-zero country digit, then up to 14 more. The column is
    #: `varchar(20)`, which is wider than the standard permits; the pattern is the real
    #: constraint.
    phone_e164: str | None = Field(default=None, max_length=20, pattern=r"^\+[1-9]\d{1,14}$")
    skills: list[str] | None = Field(default=None, max_length=64)
    responsibilities: str | None = None


def _view(profile: EmployeeProfile) -> ProfileView:
    return ProfileView(
        employee_code=profile.employee_code,
        department=profile.department,
        designation=profile.designation,
        joining_date=profile.joining_date,
        phone_e164=profile.phone_e164,
        skills=list(profile.skills),
        responsibilities=profile.responsibilities,
        updated_at=profile.updated_at,
    )


@router.get("/profile")
@requires(Permission.PROFILE_SELF_READ)
async def read_my_profile(principal: CurrentPrincipal, session: Db) -> ProfileView:
    """This caller's employee profile.

    **404 when there is none, and that is a normal state rather than a fault.** Migration
    0002 is explicit that an IT Admin or an Owner is a `users` row with no profile at all.
    Returning 200 with every field null would make "no profile" indistinguishable from "a
    profile somebody saved empty", which are different things — the second has an
    `updated_at`.
    """
    return _view(await read_profile(session, user_id=principal.user_id))


@router.patch("/profile")
@requires(Permission.PROFILE_SELF_UPDATE)
async def update_my_profile(
    payload: ProfilePatch, principal: CurrentPrincipal, session: Db
) -> ProfileView:
    """Create or patch this caller's own profile. Never anybody else's.

    The user id comes from the authenticated principal and the organisation is read from
    the session GUC inside the SQL, so neither is reachable from the request body — and
    `extra="forbid"` refuses the attempt outright rather than ignoring it silently.

    `model_fields_set` is what separates "not mentioned" from "explicitly null": a PATCH
    that omits `department` must leave it alone, while one that sends `department: null`
    must clear it. Both arrive as `None` on the model, so the value alone cannot say
    which was meant.
    """
    profile = await upsert_profile(
        session,
        user_id=principal.user_id,
        update=ProfileUpdate(
            values=payload.model_dump(),
            provided=frozenset(payload.model_fields_set),
        ),
    )
    return _view(profile)


class KnowledgeSourceCount(BaseModel):
    source_system: str
    documents: int


class RecentDocument(BaseModel):
    id: str
    title: str
    source_system: str
    created_at: datetime


class MyKnowledge(BaseModel):
    """What the caller's linked identities make readable, summarised.

    Counts and titles only, never content — the page this feeds explains what a
    person's authorized context contains, and reading any of it goes through
    retrieval with the same ACL that produced these numbers.
    """

    total_documents: int
    by_source: list[KnowledgeSourceCount]
    recent: list[RecentDocument]
    linked_identities: int


@router.get("/knowledge")
@requires(Permission.RETRIEVAL_QUERY)
async def read_my_knowledge(principal: CurrentPrincipal, session: Db) -> MyKnowledge:
    """The caller's authorized knowledge context, counted honestly.

    The ACL filter is retrieval's own predicate, inside the SQL, against principals
    resolved fresh for this request — the same three-arm rule as search, so these
    counts cannot disagree with what a query would return. Zero is a truthful and
    common answer: no linked identity means no principals means nothing readable,
    which is the §2 invariant, and the page says so in those words.
    """
    principals, groups = await scoped_acl_principals(session, user_id=principal.user_id)
    params = {"principals": list(principals), "groups": list(groups)}

    by_source = (
        await session.execute(
            text(
                "SELECT s.system AS source_system, count(*) AS documents "  # noqa: S608
                "FROM documents d JOIN sources s ON s.id = d.source_id "
                f"WHERE d.superseded_by IS NULL AND {ACL_PREDICATE} "
                "GROUP BY s.system ORDER BY documents DESC"
            ),
            params,
        )
    ).all()

    recent = (
        await session.execute(
            text(
                "SELECT d.id, d.title, d.created_at, s.system AS source_system "  # noqa: S608
                "FROM documents d JOIN sources s ON s.id = d.source_id "
                f"WHERE d.superseded_by IS NULL AND {ACL_PREDICATE} "
                "ORDER BY d.created_at DESC, d.id DESC LIMIT 10"
            ),
            params,
        )
    ).all()

    identities = (
        await session.execute(
            text(
                "SELECT count(*) FROM source_identities WHERE user_id = :user AND is_active = true"
            ),
            {"user": principal.user_id},
        )
    ).scalar_one()

    return MyKnowledge(
        total_documents=sum(row.documents for row in by_source),
        by_source=[
            KnowledgeSourceCount(source_system=row.source_system, documents=row.documents)
            for row in by_source
        ],
        recent=[
            RecentDocument(
                id=str(row.id),
                title=row.title,
                source_system=row.source_system,
                created_at=row.created_at,
            )
            for row in recent
        ],
        linked_identities=identities,
    )
