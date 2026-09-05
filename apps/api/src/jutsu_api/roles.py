"""The role taxonomy: reading the catalogue, and assigning it to an employee.

Migration 0018 owns the catalogue and `docs/adr/0015-role-taxonomy.md` argues the shape.
This module is the application half, and everything in it follows from one distinction:

**A platform role code is standing, not authority.** `MGR` says where somebody sits on
the org chart; `Role.MANAGER` does not exist, and `Role.HR_ADMIN` is a permission set
that has nothing to do with the `HRA` seat. Nothing here reads or writes `user_roles`, so
no assignment made through this module can widen what anybody may do. The confusion this
guards against is not hypothetical: `HRA`/`hr_admin` and `ITA`/`it_admin` are one
careless join apart.

Three server-side rules sit on top of the schema's foreign keys, because a foreign-key
violation is a 500 and a refusal should be a sentence:

* **The status is derived, never accepted.** A client that could send
  `role_mapping_status` could claim `mapped` over an empty row. It is computed from what
  was actually assigned, and the CHECK constraint is the backstop.
* **A privileged code needs rank, not just permission — in both directions.**
  `member:assign_role_code` is held by hr_admin, and the four governance seats are not
  hr_admin's to hand out *or to take away*: filling one and vacating one both require
  Owner or Super Admin, and neither may be done to oneself. Guarding only the grant would
  let an HR Admin strip a Chairman's seat while being unable to award one.
* **Every change is audited with its before and after**, in the same transaction as the
  write, using the same shape as `change_member_role`.

`org_id` is never a parameter from the browser. It comes from the session, RLS scopes
every statement, and a target in another tenant is simply not found.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from jutsu_core.errors import NotFound, PermissionDenied, ValidationFailed
from jutsu_core.rbac import Role
from jutsu_core.taxonomy import MappingStatus
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CatalogueCode",
    "CatalogueLevel",
    "CataloguePractice",
    "CatalogueTitle",
    "RoleAssignment",
    "RoleCatalogue",
    "TaxonomyView",
    "assign_role_taxonomy",
    "read_catalogue",
    "read_taxonomy",
]

#: Roles that may seat somebody in one of the four governance codes. Deliberately a role
#: rank question and not a permission: `member:assign_role_code` is HR's, and CHM/CEO/
#: ITA/HRA are not HR's to hand out. Owner and Super Admin are the only roles above the
#: administrators this protects.
_PRIVILEGED_ASSIGNERS = frozenset({Role.OWNER, Role.SUPER_ADMIN})


# ------------------------------------------------------------------------- catalogue


@dataclass(frozen=True, slots=True)
class CataloguePractice:
    key: str
    display_name: str
    disciplines: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class CatalogueLevel:
    key: str
    display_name: str
    rank: int
    description: str
    #: The platform code this level suggests, or None where the catalogue genuinely has
    #: no equivalent (Director). A suggestion for the admin UI, never an automatic write.
    suggested_code: str | None


@dataclass(frozen=True, slots=True)
class CatalogueTitle:
    key: str
    practice_key: str
    discipline_key: str | None
    display_name: str
    #: Every level the source admits, first being the documented default. More than one
    #: entry is the source document being ambiguous, not this catalogue hedging.
    level_keys: tuple[str, ...]
    default_level_key: str


@dataclass(frozen=True, slots=True)
class CatalogueCode:
    code: str
    display_name: str
    tier: int
    category: str
    description: str
    privileged: bool


@dataclass(frozen=True, slots=True)
class RoleCatalogue:
    practices: tuple[CataloguePractice, ...]
    levels: tuple[CatalogueLevel, ...]
    titles: tuple[CatalogueTitle, ...]
    codes: tuple[CatalogueCode, ...]


async def read_catalogue(session: AsyncSession) -> RoleCatalogue:
    """The whole taxonomy, in four statements.

    Global reference data — no tenant, no RLS, identical for every organisation — so
    there is no per-caller filtering to get wrong here. Only `active` rows are returned:
    retiring a title must stop it being offered without deleting history that profiles
    still point at.
    """
    practice_rows = (
        await session.execute(
            text("SELECT key, display_name FROM role_practices WHERE active ORDER BY sort_order")
        )
    ).all()
    discipline_rows = (
        await session.execute(
            text(
                "SELECT key, practice_key, display_name FROM role_disciplines "
                "WHERE active ORDER BY sort_order"
            )
        )
    ).all()
    level_rows = (
        await session.execute(
            text(
                "SELECT l.key, l.display_name, l.rank, l.description, c.code "
                "FROM role_levels l LEFT JOIN role_level_codes c ON c.level_key = l.key "
                "WHERE l.active ORDER BY l.rank, l.key"
            )
        )
    ).all()
    title_rows = (
        await session.execute(
            text(
                "SELECT t.key, t.practice_key, t.discipline_key, t.display_name, "
                "  array_agg(tl.level_key ORDER BY tl.is_default DESC, tl.level_key) AS levels, "
                "  min(tl.level_key) FILTER (WHERE tl.is_default) AS default_level "
                "FROM role_titles t JOIN role_title_levels tl ON tl.title_key = t.key "
                "WHERE t.active "
                "GROUP BY t.key, t.practice_key, t.discipline_key, t.display_name, t.sort_order "
                "ORDER BY t.sort_order"
            )
        )
    ).all()
    code_rows = (
        await session.execute(
            text(
                "SELECT code, display_name, tier, category, description, privileged "
                "FROM role_codes WHERE active ORDER BY tier DESC, code"
            )
        )
    ).all()

    by_practice: dict[str, list[tuple[str, str]]] = {}
    for row in discipline_rows:
        by_practice.setdefault(row.practice_key, []).append((row.key, row.display_name))

    return RoleCatalogue(
        practices=tuple(
            CataloguePractice(
                key=row.key,
                display_name=row.display_name,
                disciplines=tuple(by_practice.get(row.key, ())),
            )
            for row in practice_rows
        ),
        levels=tuple(
            CatalogueLevel(
                key=row.key,
                display_name=row.display_name,
                rank=row.rank,
                description=row.description,
                suggested_code=row.code,
            )
            for row in level_rows
        ),
        titles=tuple(
            CatalogueTitle(
                key=row.key,
                practice_key=row.practice_key,
                discipline_key=row.discipline_key,
                display_name=row.display_name,
                level_keys=tuple(row.levels),
                default_level_key=row.default_level,
            )
            for row in title_rows
        ),
        codes=tuple(
            CatalogueCode(
                code=row.code,
                display_name=row.display_name,
                tier=row.tier,
                category=row.category,
                description=row.description,
                privileged=row.privileged,
            )
            for row in code_rows
        ),
    )


# ------------------------------------------------------------------------ assignment


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    """What an administrator is asking to set.

    `provided` carries PATCH semantics the same way `ProfileUpdate` does: a field that
    was *set to null* clears it, a field that was not mentioned keeps its stored value.
    `role_mapping_status` is deliberately absent — it is derived, so a client cannot
    claim a row is mapped when it is not.
    """

    values: dict[str, str | None]
    provided: frozenset[str]


_ASSIGNABLE = (
    "practice_key",
    "role_title_key",
    "role_title_custom",
    "role_level_key",
    "role_code",
)

_SELECT_CURRENT = """
SELECT practice_key, role_title_key, role_title_custom, role_level_key, role_code,
       role_mapping_status
FROM employee_profiles WHERE user_id = :user_id
"""


def _derive_status(values: dict[str, str | None]) -> MappingStatus:
    """What the row IS, from what it holds. Never what the caller says it is."""
    if values.get("role_title_key"):
        return MappingStatus.MAPPED
    if values.get("role_title_custom"):
        return MappingStatus.CUSTOM
    return MappingStatus.UNMAPPED


async def _validate(session: AsyncSession, values: dict[str, str | None]) -> None:
    """Refuse impossible combinations with a sentence rather than a foreign key.

    The composite foreign keys from migration 0018 are the real guarantee and would
    refuse all of this anyway — but they surface as a 500 and an opaque constraint name.
    These checks exist so an administrator gets told what is wrong; they are not the
    security boundary and are not relied upon as one.
    """
    status = _derive_status(values)

    if status is MappingStatus.MAPPED:
        title_key = values["role_title_key"]
        row = (
            await session.execute(
                text(
                    "SELECT t.practice_key, "
                    "  array_agg(tl.level_key ORDER BY tl.level_key) AS levels "
                    "FROM role_titles t JOIN role_title_levels tl ON tl.title_key = t.key "
                    "WHERE t.key = :key AND t.active "
                    "GROUP BY t.practice_key"
                ),
                {"key": title_key},
            )
        ).first()
        if row is None:
            raise ValidationFailed(f"'{title_key}' is not a role title in the catalogue.")
        if values.get("practice_key") != row.practice_key:
            raise ValidationFailed(
                "That role title belongs to a different practice. "
                f"'{title_key}' is a {row.practice_key} title."
            )
        level = values.get("role_level_key")
        if not level:
            raise ValidationFailed("A role title needs a normalized level.")
        if level not in row.levels:
            raise ValidationFailed(
                f"'{level}' is not a level the catalogue admits for that title. "
                f"Allowed: {', '.join(row.levels)}."
            )

    if status is MappingStatus.CUSTOM and not values.get("role_level_key"):
        # Without a level a custom title is invisible to every cross-practice query,
        # which is the one thing the taxonomy exists to make possible.
        raise ValidationFailed("A custom role title still needs a normalized level.")

    if (level := values.get("role_level_key")) and status is MappingStatus.UNMAPPED:
        raise ValidationFailed("A normalized level needs a role title to belong to.")

    if level:
        known = (
            await session.execute(
                text("SELECT 1 FROM role_levels WHERE key = :key AND active"), {"key": level}
            )
        ).scalar_one_or_none()
        if known is None:
            raise ValidationFailed(f"'{level}' is not a seniority level in the catalogue.")


async def _code_is_privileged(session: AsyncSession, code: str) -> bool:
    row = (
        await session.execute(
            text("SELECT privileged FROM role_codes WHERE code = :code AND active"),
            {"code": code},
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValidationFailed(f"'{code}' is not a platform role code in the catalogue.")
    return bool(row)


async def assign_role_taxonomy(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    actor_role: Role,
    target_user_id: UUID,
    org_id: UUID,
    assignment: RoleAssignment,
) -> TaxonomyView:
    """Set an employee's practice, title, level and platform code.

    The target must be somebody in the caller's organisation — RLS makes a foreign
    tenant's employee indistinguishable from a typo, and the explicit existence check
    turns that into a 404 rather than a confusing conflict.

    Assigning one of the four governance seats needs Owner or Super Admin *on top of* the
    permission, and may never be done to oneself. Codes confer no permission, so this is
    not an escalation guard — it is an integrity one: a Chairman is a fact about the
    organisation, not something an administrator declares about themselves.
    """
    exists = (
        await session.execute(
            text("SELECT 1 FROM users WHERE id = :uid"), {"uid": str(target_user_id)}
        )
    ).scalar_one_or_none()
    if exists is None:
        raise NotFound("That employee was not found.")

    current = (
        await session.execute(text(_SELECT_CURRENT), {"user_id": str(target_user_id)})
    ).first()
    before = {
        column: (getattr(current, column) if current is not None else None)
        for column in _ASSIGNABLE
    }

    merged: dict[str, str | None] = dict(before)
    for column in _ASSIGNABLE:
        if column in assignment.provided:
            merged[column] = assignment.values.get(column)

    # Both directions of the change are guarded, not just the grant.
    #
    # Checking only the incoming code would let an HR Admin *strip* a Chairman's seat
    # while being unable to award one, which is the same integrity problem viewed from
    # the other end — and §16 asks for privileged removal to be a tracked act, which it
    # cannot be if any administrator can do it silently. So a privileged code being
    # vacated needs the same rank as a privileged code being filled.
    new_code = merged.get("role_code")
    old_code = before.get("role_code")
    if new_code != old_code:
        touching_governance = (
            new_code is not None and await _code_is_privileged(session, new_code)
        ) or (old_code is not None and await _code_is_privileged(session, old_code))
        if touching_governance:
            if actor_role not in _PRIVILEGED_ASSIGNERS:
                raise PermissionDenied(
                    "Only an Organisation Owner or Super Admin may change a governance role code."
                )
            if target_user_id == actor_user_id:
                raise PermissionDenied("You cannot change your own governance role code.")

    await _validate(session, merged)
    status = _derive_status(merged)

    params: dict[str, object] = {
        "user_id": str(target_user_id),
        "status": status.value,
        **merged,
    }
    await session.execute(
        text(
            """
            INSERT INTO employee_profiles (
                user_id, org_id, practice_key, role_title_key, role_title_custom,
                role_level_key, role_code, role_mapping_status
            )
            VALUES (
                :user_id,
                NULLIF(current_setting('app.current_org_id', true), '')::uuid,
                :practice_key, :role_title_key, :role_title_custom,
                :role_level_key, :role_code, :status
            )
            ON CONFLICT (user_id) DO UPDATE SET
                practice_key = EXCLUDED.practice_key,
                role_title_key = EXCLUDED.role_title_key,
                role_title_custom = EXCLUDED.role_title_custom,
                role_level_key = EXCLUDED.role_level_key,
                role_code = EXCLUDED.role_code,
                role_mapping_status = EXCLUDED.role_mapping_status,
                updated_at = now()
            """
        ),
        params,
    )

    changed = {
        column: {"from": before[column], "to": merged[column]}
        for column in _ASSIGNABLE
        if before[column] != merged[column]
    }
    if changed:
        await session.execute(
            text(
                "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
                "resource_id, outcome, meta_json) "
                "VALUES (:org, :actor, 'user', 'member.role_taxonomy_changed', 'user', :rid, "
                "'success', cast(:meta AS jsonb))"
            ),
            {
                "org": str(org_id),
                "actor": str(actor_user_id),
                "rid": str(target_user_id),
                "meta": json.dumps({"changes": changed, "status": status.value}),
            },
        )

    view = await read_taxonomy(session, user_id=target_user_id)
    if view is None:  # pragma: no cover - the upsert above guarantees a row
        raise NotFound("That employee was not found.")
    return view


# ------------------------------------------------------------------------------ read


@dataclass(frozen=True, slots=True)
class TaxonomyView:
    """One employee's role taxonomy, with the catalogue's display names resolved.

    Joined rather than keyed so a caller rendering one person does not have to fetch the
    whole catalogue to spell "Senior Consultant". `role_title` is whichever of the
    catalogue title and the custom title actually applies, so the reader does not have to
    know which path produced it.
    """

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


_SELECT_VIEW = """
SELECT ep.practice_key, p.display_name AS practice,
       d.display_name AS discipline,
       ep.role_title_key,
       COALESCE(t.display_name, ep.role_title_custom) AS role_title,
       ep.role_level_key, l.display_name AS role_level, l.rank AS role_level_rank,
       ep.role_code, c.display_name AS role_code_name, c.tier AS role_code_tier,
       ep.role_mapping_status
FROM employee_profiles ep
LEFT JOIN role_practices p ON p.key = ep.practice_key
LEFT JOIN role_titles t ON t.key = ep.role_title_key
LEFT JOIN role_disciplines d ON d.key = t.discipline_key
LEFT JOIN role_levels l ON l.key = ep.role_level_key
LEFT JOIN role_codes c ON c.code = ep.role_code
WHERE ep.user_id = :user_id
"""


async def read_taxonomy(session: AsyncSession, *, user_id: UUID) -> TaxonomyView | None:
    """One employee's taxonomy, or None where they have no profile row at all.

    None rather than `NotFound`: a person with no profile is an ordinary state (migration
    0002 says an Owner may legitimately have none), and the callers differ on what to do
    about it — the admin console shows an empty assignment, the profile page shows the
    rest of the profile regardless.
    """
    row = (await session.execute(text(_SELECT_VIEW), {"user_id": str(user_id)})).first()
    if row is None:
        return None
    return TaxonomyView(
        practice_key=row.practice_key,
        practice=row.practice,
        discipline=row.discipline,
        role_title_key=row.role_title_key,
        role_title=row.role_title,
        role_level_key=row.role_level_key,
        role_level=row.role_level,
        role_level_rank=row.role_level_rank,
        role_code=row.role_code,
        role_code_name=row.role_code_name,
        role_code_tier=row.role_code_tier,
        mapping_status=row.role_mapping_status,
    )
