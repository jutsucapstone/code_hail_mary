"""The code catalogue and the seeded catalogue must be the same thing.

Migration 0002 seeds `roles`, `permissions` and `role_permissions`, then revokes write
access to them from the application role. That makes the database the runtime authority:
a compromised request path cannot mint a role or widen a permission, because only a
migration can write those tables.

The cost of that design is a second copy — `jutsu_core.rbac` — which routes and the
generated TypeScript client are authored against. These tests are what stop the two
drifting. Without them the failure is silent and specific: a permission added in code but
never seeded means every check against it denies, and a permission seeded but removed
from code means a role quietly keeps a power nobody can see in the source.
"""

from __future__ import annotations

import pytest
from jutsu_core.rbac import ROLE_PERMISSIONS, ROLE_RANKS, Permission, Role, outranks
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class TestCatalogueMatchesTheDatabase:
    async def test_roles_are_identical(self, conn: AsyncConnection) -> None:
        rows = (await conn.execute(text("SELECT key, rank FROM roles"))).all()
        seeded = {key: rank for key, rank in rows}

        assert seeded == {role.value: rank for role, rank in ROLE_RANKS.items()}

    async def test_permissions_are_identical(self, conn: AsyncConnection) -> None:
        seeded = set((await conn.execute(text("SELECT key FROM permissions"))).scalars().all())

        assert seeded == {permission.value for permission in Permission}

    async def test_the_matrix_is_identical(self, conn: AsyncConnection) -> None:
        rows = (
            await conn.execute(text("SELECT role_key, permission_key FROM role_permissions"))
        ).all()

        seeded: dict[str, set[str]] = {}
        for role_key, permission_key in rows:
            seeded.setdefault(role_key, set()).add(permission_key)

        in_code = {
            role.value: {permission.value for permission in permissions}
            for role, permissions in ROLE_PERMISSIONS.items()
        }
        assert seeded == in_code


class TestCatalogueInvariants:
    """Properties that must hold however the matrix is edited."""

    def test_every_role_has_a_rank(self) -> None:
        assert set(ROLE_RANKS) == set(Role)

    def test_every_role_has_an_entry_in_the_matrix(self) -> None:
        assert set(ROLE_PERMISSIONS) == set(Role)

    def test_no_permission_grants_document_visibility(self) -> None:
        """The control plane must not be able to widen the data plane.

        §17: "Roles gate features; ACLs gate data. Never conflate them." Evidence
        visibility is decided by `document_acl.principal_id` matching the caller's
        namespaced source-identity subjects (ADR 0010 — it was `users.external_id` when
        this test was written, and that column no longer means anything). If a permission
        ever appears here that implies reading documents, chunks or evidence directly, a
        feature has reached into the product invariant and this test is the tripwire.

        **It has already caught one.** S7's retrieval permission was drafted as
        `evidence:read` and refused here. The refusal was right: a permission spelled
        after a data-plane object reads as a grant over that object however the docstring
        argues otherwise, and the next person to add one follows the precedent. It became
        `retrieval:query` — the query is the feature, the evidence is not.
        """
        forbidden = {"document", "chunk", "evidence", "memory", "acl"}
        offenders = {
            permission.value
            for permission in Permission
            if any(word in permission.value.lower() for word in forbidden)
        }
        assert not offenders, (
            f"control-plane permissions must not name data-plane objects: {offenders}"
        )

    def test_owner_is_the_only_role_that_can_delete_the_organisation(self) -> None:
        holders = {
            role
            for role, permissions in ROLE_PERMISSIONS.items()
            if Permission.ORG_DELETE in permissions
        }
        assert holders == {Role.OWNER}

    def test_every_role_can_manage_its_own_profile(self) -> None:
        """Self-service is not a privilege; a Member with no other power still has it."""
        for role, permissions in ROLE_PERMISSIONS.items():
            assert Permission.PROFILE_SELF_READ in permissions, role
            assert Permission.PROFILE_SELF_UPDATE in permissions, role
            assert Permission.INTEGRATION_SELF_MANAGE in permissions, role

    def test_every_role_can_read_its_own_identity(self) -> None:
        """GET /v1/me must work for a bare Member.

        It was gated on ORG_READ, which `member` does not hold — so an invited employee
        could sign in successfully and then be refused the identity the shell needs
        before it can render at all.
        """
        for role, permissions in ROLE_PERMISSIONS.items():
            assert Permission.PROFILE_SELF_READ in permissions, role

    def test_member_has_nothing_but_self_service(self) -> None:
        """A Member holds exactly what *every* role holds, and nothing beyond it.

        Stated as an intersection rather than only as a list, because the list was the
        weaker half: it had to be edited whenever a universal permission was added, and an
        edit made to turn a test green is an edit that stops asking the original question.
        The intersection cannot drift — it says a Member is the floor, whatever the floor
        becomes — and the list below then pins what that floor currently is.
        """
        universal = set.intersection(*(set(held) for held in ROLE_PERMISSIONS.values()))

        assert set(ROLE_PERMISSIONS[Role.MEMBER]) == universal, (
            "a Member holds something not every role holds"
        )
        assert universal == {
            Permission.PROFILE_SELF_READ,
            Permission.PROFILE_SELF_UPDATE,
            Permission.INTEGRATION_SELF_MANAGE,
            # Retrieval. Universal on purpose: asking the corporate memory a question is
            # the feature every employee is hired to use, and `document_acl` decides what
            # comes back. Gating it on rank would make a role decide what a person may
            # read, which is the §17 conflation the test above exists to prevent.
            Permission.RETRIEVAL_QUERY,
            # Opening a KT package addressed to you (migration 0013). Universal because
            # the typical recipient is a brand-new Member; the package's own binding —
            # recipient, expiry, revocation — is the actual gate, and holding this
            # grants no document.
            Permission.KT_OPEN,
        }


class TestEscalationCeiling:
    def test_nobody_outranks_themselves(self) -> None:
        """The check is strict, so no actor can act on their own level.

        This is what stops an admin promoting someone to their own rank and then being
        unable to undo it — or being outranked by their own grant.
        """
        for role in Role:
            assert not outranks(role, role)

    def test_peers_cannot_act_on_each_other(self) -> None:
        """HR Admin and IT Admin are equals with disjoint powers.

        Neither may promote anyone into the other's role. A unique rank column would
        have invented an order here and handed one of them authority over the other.
        """
        assert ROLE_RANKS[Role.HR_ADMIN] == ROLE_RANKS[Role.IT_ADMIN]
        assert not outranks(Role.HR_ADMIN, Role.IT_ADMIN)
        assert not outranks(Role.IT_ADMIN, Role.HR_ADMIN)

    def test_owner_outranks_every_other_role(self) -> None:
        for role in Role:
            if role is not Role.OWNER:
                assert outranks(Role.OWNER, role)

    @pytest.mark.parametrize("role", [r for r in Role if r is not Role.OWNER])
    def test_no_role_outranks_the_owner(self, role: Role) -> None:
        assert not outranks(role, Role.OWNER)

    def test_a_role_with_fewer_powers_never_outranks_one_with_more(self) -> None:
        """Rank and capability must not disagree.

        A role that outranks another while holding a strict subset of its permissions is
        an escalation path: the higher-ranked actor can grant the lower role, then be
        acted upon by a permission they do not themselves hold.
        """
        for high in Role:
            for low in Role:
                if outranks(high, low):
                    assert not ROLE_PERMISSIONS[low] > ROLE_PERMISSIONS[high], (
                        f"{low} outranks-by-capability {high} while {high} outranks {low} by rank"
                    )
