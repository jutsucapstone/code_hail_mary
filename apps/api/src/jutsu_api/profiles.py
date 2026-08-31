"""The employee's own profile row (§17, spec §8).

`employee_profiles` has existed since migration 0002 — RLS enabled and FORCED, a
composite foreign key to `(users.id, users.org_id)` — and until now nothing in the
application read or wrote it. The permissions were catalogued too: `profile:self_read`
and `profile:self_update` are both in `_EVERYONE`, so every role already holds them.
This module is the missing half.

**Neither the tenant nor the user is ever a parameter from the browser.** The user id
comes from the authenticated `Principal`, and `org_id` is read *inside the SQL* from
`app.current_org_id` — the same GUC the row-level policy compares against. A caller
therefore cannot write a row into another organisation even by asking: the `WITH CHECK`
clause on `employee_profiles_org_isolation` refuses it, and there is no code path that
would carry a client-supplied value that far.

**A profile is legitimately absent.** Migration 0002 says so directly: "An IT Admin or
Organization Owner is a `users` row with NO profile — department, designation and joining
date are meaningfully NOT NULL only for employees." So reading one that does not exist is
`NotFound`, and the first `PATCH` creates it. The primary key is `user_id`, so the upsert
below cannot produce a second profile for the same person however many times it runs.

Raw parameterised SQL rather than the ORM, matching `me.py` and `identities.py`. There is
no `EmployeeProfile` mapped class and this module deliberately does not add one: the API
layer reads through the RLS-scoped session everywhere, and a second data-access idiom for
one table would be a worse trade than the repetition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from jutsu_core.errors import NotFound
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["EmployeeProfile", "ProfileUpdate", "read_profile", "upsert_profile"]

#: Every column a caller may see or set. `user_id` and `org_id` are deliberately absent:
#: they are identity, not profile data, and the caller already knows their own from
#: `GET /v1/me`.
_COLUMNS = (
    "employee_code",
    "department",
    "designation",
    "joining_date",
    "phone_e164",
    "skills",
    "responsibilities",
)

_SELECT = (
    f"SELECT {', '.join(_COLUMNS)}, updated_at FROM employee_profiles WHERE user_id = :user_id"  # noqa: S608 - column list is a module constant, never caller input
)

#: One statement: insert if absent, patch the named fields if present.
#:
#: The `:set_*` flags are what make this a PATCH rather than a replace — a column the
#: caller did not mention keeps its stored value instead of being nulled. Doing it in SQL
#: rather than by read-modify-write also closes the lost-update window between two
#: concurrent saves from the same person.
#:
#: `org_id` is taken from the GUC **inside the statement**. It is never bound from Python,
#: so there is no parameter a route could accidentally thread a client value into, and the
#: policy's WITH CHECK clause validates the result regardless.
_UPSERT = """
INSERT INTO employee_profiles (
    user_id, org_id, employee_code, department, designation,
    joining_date, phone_e164, skills, responsibilities
)
VALUES (
    :user_id,
    NULLIF(current_setting('app.current_org_id', true), '')::uuid,
    :employee_code, :department, :designation,
    CAST(:joining_date AS date), :phone_e164, CAST(:skills AS text[]), :responsibilities
)
ON CONFLICT (user_id) DO UPDATE SET
    employee_code = CASE WHEN :set_employee_code
        THEN EXCLUDED.employee_code ELSE employee_profiles.employee_code END,
    department = CASE WHEN :set_department
        THEN EXCLUDED.department ELSE employee_profiles.department END,
    designation = CASE WHEN :set_designation
        THEN EXCLUDED.designation ELSE employee_profiles.designation END,
    joining_date = CASE WHEN :set_joining_date
        THEN EXCLUDED.joining_date ELSE employee_profiles.joining_date END,
    phone_e164 = CASE WHEN :set_phone_e164
        THEN EXCLUDED.phone_e164 ELSE employee_profiles.phone_e164 END,
    skills = CASE WHEN :set_skills
        THEN EXCLUDED.skills ELSE employee_profiles.skills END,
    responsibilities = CASE WHEN :set_responsibilities
        THEN EXCLUDED.responsibilities ELSE employee_profiles.responsibilities END,
    updated_at = now()
RETURNING employee_code, department, designation, joining_date,
          phone_e164, skills, responsibilities, updated_at
"""


@dataclass(frozen=True, slots=True)
class EmployeeProfile:
    """One profile row, exactly the columns `employee_profiles` has."""

    employee_code: str | None
    department: str | None
    designation: str | None
    joining_date: date | None
    phone_e164: str | None
    skills: tuple[str, ...]
    responsibilities: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProfileUpdate:
    """A partial update. `None` in a field that was *set* means "clear it".

    The two states a PATCH must distinguish — "leave this alone" and "set this to
    nothing" — are carried by `provided` rather than by the value, because `None` alone
    cannot express both.
    """

    values: dict[str, object]
    provided: frozenset[str]


def _row(row: object) -> EmployeeProfile:
    return EmployeeProfile(
        employee_code=row.employee_code,  # type: ignore[attr-defined]
        department=row.department,  # type: ignore[attr-defined]
        designation=row.designation,  # type: ignore[attr-defined]
        joining_date=row.joining_date,  # type: ignore[attr-defined]
        phone_e164=row.phone_e164,  # type: ignore[attr-defined]
        skills=tuple(row.skills or ()),  # type: ignore[attr-defined]
        responsibilities=row.responsibilities,  # type: ignore[attr-defined]
        updated_at=row.updated_at,  # type: ignore[attr-defined]
    )


async def read_profile(session: AsyncSession, *, user_id: UUID) -> EmployeeProfile:
    """This caller's profile, or `NotFound`.

    Two filters, and both are load-bearing. Row-level security scopes the statement to
    the caller's organisation; `user_id` scopes it to the caller. Either alone would be
    a defect: without the policy a colleague in another tenant with a colliding id
    becomes reachable, and without the predicate every employee in *this* tenant does.
    """
    row = (await session.execute(text(_SELECT), {"user_id": str(user_id)})).first()
    if row is None:
        raise NotFound("You do not have an employee profile yet.")
    return _row(row)


async def upsert_profile(
    session: AsyncSession, *, user_id: UUID, update: ProfileUpdate
) -> EmployeeProfile:
    """Create this caller's profile, or patch the fields they named.

    Idempotent on `user_id`, which is the primary key — running it twice updates, it
    never produces a second row for one person.
    """
    params: dict[str, object] = {"user_id": str(user_id)}
    for column in _COLUMNS:
        params[column] = update.values.get(column)
        params[f"set_{column}"] = column in update.provided

    # NOT NULL with a `{}` default, so a create that does not mention skills must still
    # bind an array rather than NULL. The `set_skills` flag keeps the update path honest.
    if params["skills"] is None:
        params["skills"] = []

    row = (await session.execute(text(_UPSERT), params)).one()
    return _row(row)
