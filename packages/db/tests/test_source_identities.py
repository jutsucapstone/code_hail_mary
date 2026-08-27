"""Source identities and group membership as an authorisation boundary (ADR 0010).

These are the database-layer half of S6.5. They run against a real Postgres as the
restricted `jutsu_app` role, because row-level security is the thing under test and a
superuser bypasses it unconditionally — the failure ADR 0003 records.

The property throughout: **an ACL principal a caller does not legitimately hold must be
unreachable, and the failure must be silence rather than an error.** A cross-tenant read
that raised would still confirm the row exists; §17.6 treats that as a leak too.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection


async def scope(conn: AsyncConnection, org_id: uuid.UUID) -> None:
    await conn.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )


async def make_org_with_user(conn: AsyncConnection, label: str) -> tuple[uuid.UUID, uuid.UUID]:
    """One organisation and one user in it, created under that organisation's scope.

    The GUC is set before the organisation row exists, which is the same ordering real
    registration uses: `orgs` carries a `WITH CHECK` on `id`, so the row cannot be scoped
    to itself after the fact.
    """
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    await scope(conn, org_id)
    await conn.execute(
        text("INSERT INTO orgs (id, name) VALUES (:id, :name)"), {"id": org_id, "name": label}
    )
    await conn.execute(
        text("INSERT INTO users (id, org_id, email, status) VALUES (:id, :org, :email, 'active')"),
        {"id": user_id, "org": org_id, "email": f"{label}@example.com"},
    )
    return org_id, user_id


async def link(
    conn: AsyncConnection,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    system: str,
    subject: str,
    *,
    active: bool = True,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO source_identities (org_id, user_id, source_system, subject, is_active) "
            "VALUES (:org, :user, CAST(:system AS source_system), :subject, :active)"
        ),
        {"org": org_id, "user": user_id, "system": system, "subject": subject, "active": active},
    )


async def principals(conn: AsyncConnection, user_id: uuid.UUID) -> set[str]:
    """What the application's resolution query would return, run at the SQL layer."""
    rows = (
        await conn.execute(
            text(
                "SELECT source_system, subject FROM source_identities "
                "WHERE user_id = :u AND is_active"
            ),
            {"u": user_id},
        )
    ).all()
    return {f"{system}:{subject}" for system, subject in rows}


# --------------------------------------------------------------------------- schema


class TestSchema:
    async def test_both_new_tables_have_rls_enabled_and_forced(self, conn: AsyncConnection) -> None:
        """FORCE is the half that is easy to omit, and the half that matters (ADR 0003)."""
        for table in ("source_identities", "user_groups"):
            row = (
                await conn.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = :t"
                    ),
                    {"t": table},
                )
            ).one()
            assert row.relrowsecurity is True, f"{table}: RLS not enabled"
            assert row.relforcerowsecurity is True, f"{table}: RLS not FORCED"

    async def test_the_subject_is_unique_within_an_organisation(
        self, conn: AsyncConnection
    ) -> None:
        org, user = await make_org_with_user(conn, "alpha")
        await link(conn, org, user, "slack", "U01ABC")
        with pytest.raises(DBAPIError):
            await link(conn, org, user, "slack", "U01ABC")
        await conn.rollback()

    async def test_user_groups_carries_an_org_id(self, conn: AsyncConnection) -> None:
        """It shipped without one, which left an authorisation input unscoped."""
        row = (
            await conn.execute(
                text(
                    "SELECT count(*) AS n FROM information_schema.columns "
                    "WHERE table_name = 'user_groups' AND column_name = 'org_id'"
                )
            )
        ).one()
        assert row.n == 1


# --------------------------------------------------------------------------- identities


class TestIdentityResolution:
    async def test_one_provider_identity(self, conn: AsyncConnection) -> None:
        org, user = await make_org_with_user(conn, "alpha")
        await link(conn, org, user, "slack", "U01ABC")
        assert await principals(conn, user) == {"slack:U01ABC"}

    async def test_multiple_provider_identities(self, conn: AsyncConnection) -> None:
        """The cardinality `users.external_id` could not express."""
        org, user = await make_org_with_user(conn, "alpha")
        await link(conn, org, user, "slack", "U01ABC")
        await link(conn, org, user, "github", "12345")
        await link(conn, org, user, "local", "ada@example.com")

        assert await principals(conn, user) == {
            "slack:U01ABC",
            "github:12345",
            "local:ada@example.com",
        }

    async def test_the_same_subject_in_two_providers_is_two_principals(
        self, conn: AsyncConnection
    ) -> None:
        """`12345` in Slack and `12345` in GitHub are different people.

        Without the namespace they would be one string, and a grant issued by one system
        would authorise a principal from the other.
        """
        org, user = await make_org_with_user(conn, "alpha")
        await link(conn, org, user, "slack", "12345")
        await link(conn, org, user, "github", "12345")

        assert await principals(conn, user) == {"slack:12345", "github:12345"}

    async def test_no_source_identity_means_no_principals(self, conn: AsyncConnection) -> None:
        """Fail-closed, and it is the default state of every newly invited user."""
        _org, user = await make_org_with_user(conn, "alpha")
        assert await principals(conn, user) == set()

    async def test_an_inactive_identity_does_not_authorise(self, conn: AsyncConnection) -> None:
        org, user = await make_org_with_user(conn, "alpha")
        await link(conn, org, user, "slack", "U01ABC", active=False)
        assert await principals(conn, user) == set()

    async def test_revocation_takes_effect_on_the_next_read(self, conn: AsyncConnection) -> None:
        """§17 test 3's requirement, applied to identities: no cache to flush."""
        org, user = await make_org_with_user(conn, "alpha")
        await link(conn, org, user, "slack", "U01ABC")
        assert await principals(conn, user) == {"slack:U01ABC"}

        await conn.execute(
            text(
                "UPDATE source_identities SET is_active = false, revoked_at = now() "
                "WHERE user_id = :u"
            ),
            {"u": user},
        )
        assert await principals(conn, user) == set()

    async def test_an_email_change_does_not_change_authorisation(
        self, conn: AsyncConnection
    ) -> None:
        """The strongest practical argument for subjects over addresses.

        With emails as principals, changing an address strands every `document_acl` row
        naming the old one — access silently disappears and nothing reports it.
        """
        org, user = await make_org_with_user(conn, "alpha")
        await link(conn, org, user, "slack", "U01ABC")
        before = await principals(conn, user)

        await conn.execute(
            text("UPDATE users SET email = :email WHERE id = :u"),
            {"email": "renamed@example.com", "u": user},
        )
        assert await principals(conn, user) == before


# --------------------------------------------------------------------------- tenancy


class TestCrossTenantIsolation:
    async def test_the_same_subject_may_exist_in_two_organisations(
        self, conn: AsyncConnection
    ) -> None:
        """Two tenants connecting the same Slack workspace is legitimate.

        This is why the unique constraint is `(org_id, source_system, subject)` and not
        global — a global one would make the second tenant's link fail.
        """
        org_a, user_a = await make_org_with_user(conn, "alpha")
        await link(conn, org_a, user_a, "slack", "U-SHARED")
        org_b, user_b = await make_org_with_user(conn, "beta")
        await link(conn, org_b, user_b, "slack", "U-SHARED")

        await scope(conn, org_a)
        assert await principals(conn, user_a) == {"slack:U-SHARED"}
        await scope(conn, org_b)
        assert await principals(conn, user_b) == {"slack:U-SHARED"}

    async def test_a_source_identity_is_invisible_from_another_tenant(
        self, conn: AsyncConnection
    ) -> None:
        """Adversarial: org B holds org A's user id and asks for its identities."""
        org_a, user_a = await make_org_with_user(conn, "alpha")
        await link(conn, org_a, user_a, "slack", "U01ABC")
        org_b, _user_b = await make_org_with_user(conn, "beta")

        await scope(conn, org_b)
        assert await principals(conn, user_a) == set(), "another tenant's identity was visible"

    async def test_an_unscoped_session_sees_no_identities(self, conn: AsyncConnection) -> None:
        """Forgetting to scope must fail closed, not open."""
        org, user = await make_org_with_user(conn, "alpha")
        await link(conn, org, user, "slack", "U01ABC")

        await conn.execute(text("SELECT set_config('app.current_org_id', '', true)"))
        assert await principals(conn, user) == set()

    async def test_an_identity_cannot_be_written_into_another_tenant(
        self, conn: AsyncConnection
    ) -> None:
        """`WITH CHECK` refuses the insert rather than accepting an unreachable row."""
        org_a, user_a = await make_org_with_user(conn, "alpha")
        org_b, _user_b = await make_org_with_user(conn, "beta")

        await scope(conn, org_b)
        with pytest.raises(DBAPIError):
            await link(conn, org_a, user_a, "slack", "U-SMUGGLED")
        await conn.rollback()

    async def test_identity_counts_do_not_leak(self, conn: AsyncConnection) -> None:
        """§17.6 — a count that reflects unreadable rows is the same disclosure."""
        org_a, user_a = await make_org_with_user(conn, "alpha")
        await link(conn, org_a, user_a, "slack", "U-A1")
        await link(conn, org_a, user_a, "github", "A2")
        org_b, user_b = await make_org_with_user(conn, "beta")
        await link(conn, org_b, user_b, "slack", "U-B1")

        await scope(conn, org_a)
        count_a = (await conn.execute(text("SELECT count(*) FROM source_identities"))).scalar_one()
        await scope(conn, org_b)
        count_b = (await conn.execute(text("SELECT count(*) FROM source_identities"))).scalar_one()

        assert (count_a, count_b) == (2, 1)


# --------------------------------------------------------------------------- groups


class TestGroupMembership:
    async def test_a_group_row_is_scoped_to_its_organisation(self, conn: AsyncConnection) -> None:
        org_a, user_a = await make_org_with_user(conn, "alpha")
        await conn.execute(
            text(
                "INSERT INTO user_groups (user_id, org_id, group_external_id) "
                "VALUES (:u, :o, 'slack:S-ALPHA')"
            ),
            {"u": user_a, "o": org_a},
        )
        org_b, _user_b = await make_org_with_user(conn, "beta")

        await scope(conn, org_b)
        rows = (await conn.execute(text("SELECT group_external_id FROM user_groups"))).all()
        assert rows == [], "another tenant's group membership was visible"

    async def test_group_membership_cannot_be_written_across_organisations(
        self, conn: AsyncConnection
    ) -> None:
        org_a, user_a = await make_org_with_user(conn, "alpha")
        org_b, _user_b = await make_org_with_user(conn, "beta")

        await scope(conn, org_b)
        with pytest.raises(DBAPIError):
            await conn.execute(
                text(
                    "INSERT INTO user_groups (user_id, org_id, group_external_id) "
                    "VALUES (:u, :o, 'slack:S-SMUGGLED')"
                ),
                {"u": user_a, "o": org_a},
            )
        await conn.rollback()

    async def test_removing_a_group_takes_effect_on_the_next_read(
        self, conn: AsyncConnection
    ) -> None:
        """§17 test 3, literally: no cache flush, next query, gone."""
        org, user = await make_org_with_user(conn, "alpha")
        await conn.execute(
            text(
                "INSERT INTO user_groups (user_id, org_id, group_external_id) "
                "VALUES (:u, :o, 'slack:S-ALPHA')"
            ),
            {"u": user, "o": org},
        )

        async def groups() -> set[str]:
            rows = (
                await conn.execute(
                    text("SELECT group_external_id FROM user_groups WHERE user_id = :u"),
                    {"u": user},
                )
            ).all()
            return {str(row[0]) for row in rows}

        assert await groups() == {"slack:S-ALPHA"}
        await conn.execute(text("DELETE FROM user_groups WHERE user_id = :u"), {"u": user})
        assert await groups() == set()
