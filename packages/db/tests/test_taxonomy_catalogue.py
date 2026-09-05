"""The taxonomy in code and the taxonomy in the database must be the same thing.

Migration 0018 seeds seven catalogue tables and then revokes write access to them from
the application role, so the database is the runtime authority: a compromised request
path cannot mint a role code or widen which levels a title admits. The cost of that
design is a second copy — `jutsu_core.taxonomy` — which the API and the generated
TypeScript client are authored against. These tests are what stop the two drifting.

The failure they prevent is quiet and specific. A title added in code but never seeded
means every assignment against it is refused by a foreign key nobody expected to fire; a
level seeded but removed from code means the API cannot name a seniority the database
still holds; and a `LEVEL_TO_CODE` entry that disagrees with `role_level_codes` means the
admin console suggests one code while the catalogue documents another.
"""

from __future__ import annotations

import pytest
from jutsu_core.rbac import Role
from jutsu_core.taxonomy import (
    CODES,
    DISCIPLINES,
    LEVEL_TO_CODE,
    LEVELS,
    PRACTICES,
    PRIVILEGED_CODES,
    TITLES,
    RoleCode,
    RoleLevel,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class TestCatalogueMatchesTheDatabase:
    async def test_practices_are_identical(self, conn: AsyncConnection) -> None:
        rows = (await conn.execute(text("SELECT key, display_name FROM role_practices"))).all()

        assert {key: name for key, name in rows} == {
            practice.value: name for practice, name in PRACTICES.items()
        }

    async def test_disciplines_are_identical(self, conn: AsyncConnection) -> None:
        rows = (
            await conn.execute(text("SELECT key, practice_key, display_name FROM role_disciplines"))
        ).all()

        assert {key: (practice, name) for key, practice, name in rows} == {
            discipline.value: (practice.value, name)
            for discipline, (practice, name) in DISCIPLINES.items()
        }

    async def test_levels_are_identical(self, conn: AsyncConnection) -> None:
        rows = (await conn.execute(text("SELECT key, display_name, rank FROM role_levels"))).all()

        assert {key: (name, rank) for key, name, rank in rows} == {
            level.value: (entry.display_name, entry.rank) for level, entry in LEVELS.items()
        }

    async def test_titles_are_identical(self, conn: AsyncConnection) -> None:
        rows = (
            await conn.execute(
                text("SELECT key, practice_key, discipline_key, display_name FROM role_titles")
            )
        ).all()

        assert {key: (practice, discipline, name) for key, practice, discipline, name in rows} == {
            key: (
                entry.practice.value,
                entry.discipline.value if entry.discipline is not None else None,
                entry.display_name,
            )
            for key, entry in TITLES.items()
        }

    async def test_the_title_level_matrix_is_identical(self, conn: AsyncConnection) -> None:
        """Including the ambiguity: a title with two admitted levels has two rows."""
        rows = (
            await conn.execute(text("SELECT title_key, level_key FROM role_title_levels"))
        ).all()

        seeded: dict[str, set[str]] = {}
        for title_key, level_key in rows:
            seeded.setdefault(title_key, set()).add(level_key)

        assert seeded == {
            key: {level.value for level in entry.levels} for key, entry in TITLES.items()
        }

    async def test_the_default_level_is_the_first_the_source_lists(
        self, conn: AsyncConnection
    ) -> None:
        rows = (
            await conn.execute(
                text("SELECT title_key, level_key FROM role_title_levels WHERE is_default")
            )
        ).all()

        assert {title_key: level_key for title_key, level_key in rows} == {
            key: entry.levels[0].value for key, entry in TITLES.items()
        }

    async def test_codes_are_identical(self, conn: AsyncConnection) -> None:
        rows = (
            await conn.execute(
                text("SELECT code, display_name, tier, category, privileged FROM role_codes")
            )
        ).all()

        assert {
            code: (name, tier, category, privileged)
            for code, name, tier, category, privileged in rows
        } == {
            code.value: (
                entry.display_name,
                entry.tier,
                entry.category.value,
                entry.privileged,
            )
            for code, entry in CODES.items()
        }

    async def test_the_level_to_code_policy_is_identical(self, conn: AsyncConnection) -> None:
        rows = (await conn.execute(text("SELECT level_key, code FROM role_level_codes"))).all()

        assert {level_key: code for level_key, code in rows} == {
            level.value: (code.value if code is not None else None)
            for level, code in LEVEL_TO_CODE.items()
        }


class TestCatalogueInvariants:
    """Properties that must hold however the taxonomy is edited."""

    def test_every_title_names_at_least_one_level(self) -> None:
        """A title with no level is invisible to every cross-practice query, which is
        the one thing this taxonomy exists to make possible."""
        assert all(entry.levels for entry in TITLES.values())

    def test_every_title_level_exists_in_the_level_catalogue(self) -> None:
        for key, entry in TITLES.items():
            assert all(level in LEVELS for level in entry.levels), key

    def test_a_discipline_belongs_to_its_titles_practice(self) -> None:
        for key, entry in TITLES.items():
            if entry.discipline is not None:
                assert DISCIPLINES[entry.discipline][0] == entry.practice, key

    def test_every_level_has_a_mapping_decision(self) -> None:
        """Including the explicit `None` for Director. A level missing from the policy
        would be an undecided mapping wearing the appearance of a decided one."""
        assert set(LEVEL_TO_CODE) == set(RoleLevel)

    def test_director_has_no_platform_code(self) -> None:
        """The documented gap. The JUTSU catalogue jumps SMR (T5) to PTR (T6), so any
        mapping for Director would invent a promotion or a demotion."""
        assert LEVEL_TO_CODE[RoleLevel.DIRECTOR] is None

    def test_no_business_level_maps_to_a_governance_seat(self) -> None:
        """The load-bearing one. A Manager is not an IT Admin Controller because both
        happen to be senior; CHM/CEO/ITA/HRA are never implied by business seniority."""
        assert not {code for code in LEVEL_TO_CODE.values() if code is not None} & PRIVILEGED_CODES

    def test_exactly_the_four_documented_seats_are_privileged(self) -> None:
        assert PRIVILEGED_CODES == {RoleCode.CHM, RoleCode.CEO, RoleCode.ITA, RoleCode.HRA}

    def test_a_role_code_is_never_an_rbac_role(self) -> None:
        """The separation the whole feature is shaped around, asserted rather than
        assumed. If these vocabularies ever shared a value, a permission check written
        against one could be satisfied by the other — and `HRA`/`hr_admin`,
        `ITA`/`it_admin` are exactly the pair a reader would conflate.
        """
        codes = {code.value.lower() for code in RoleCode}
        roles = {role.value.lower() for role in Role}
        assert not codes & roles

    @pytest.mark.parametrize("level", sorted(RoleLevel))
    def test_every_level_has_a_rank_and_a_label(self, level: RoleLevel) -> None:
        entry = LEVELS[level]
        assert entry.display_name
        assert entry.rank > 0
