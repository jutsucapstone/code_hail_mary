"""Tenant isolation and the identity boundary introduced by migration 0002.

Three things are under test here, and each exists because the corresponding control
looked correct on paper and was not.

**The negatives nobody wrote.** `test_rls.py` proves a scoped session reads only its own
rows, that counts do not leak, that an unscoped session sees nothing, and that WITH CHECK
stops a cross-org INSERT. It never tested UPDATE, DELETE or a JOIN. A policy can be
correct for SELECT and wrong for the rest — `USING` governs which rows are visible to a
modifying statement, and a missing predicate there is a silent cross-tenant write, not an
error. Those three cases are the first class below.

**Coverage that cannot go stale.** The shipped suite parametrised over a hardcoded
`RLS_TABLES`, so a new table simply was not tested — the list is the thing most likely to
be forgotten. `TestRlsCoverage` inverts it: enumerate `pg_class` and require every table
to be accounted for, so adding one without protecting it fails the build.

**A privilege boundary, not a convention.** The `auth` schema cannot be protected by RLS
(it is org-less by necessity), so it is protected by ownership and EXECUTE grants. The
tests below assert both halves: `jutsu_app` cannot touch the tables, and the four definer
functions still work. Getting only the first half right is a login path that fails closed
and reads as "user not found".
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

#: Tables carrying `org_id` that migration 0002 did NOT protect. Every one predates this
#: work; backfilling them is migration 0004 in the plan. They are listed rather than
#: ignored so the gap is visible in the test output instead of being silently absent —
#: `pii_vault` in particular holds masked-value material and is unprotected today.
RLS_PENDING_BACKFILL = frozenset(
    {
        # Nullable org_id by design: harness runs are not tenant rows, and scoping a
        # table whose rows may legitimately have no tenant is a different decision
        # from a backfill. The only remaining holdout — 0010 covered sources/jobs,
        # 0014 covered pii_vault and the extraction/resolution tables.
        "eval_runs",
    }
)

#: Tables with no tenant column at all, so there is nothing to scope by. `roles`,
#: `permissions` and `role_permissions` are the migration-owned catalogue — they are
#: global by design and are made read-only to the application instead.
RLS_EXEMPT_NO_TENANT = frozenset(
    {
        "alembic_version",
        "roles",
        "permissions",
        "role_permissions",
        "eval_results",
    }
)

AUTH_TABLES = ("identities", "login_challenges", "sessions", "jutsu_ids")


async def _scope(conn: AsyncConnection, org_id: uuid.UUID) -> None:
    await conn.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )


class TestCrossTenantWrites:
    """The cases the shipped suite never covered."""

    async def test_cross_tenant_update_touches_nothing(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Scoped to A, an UPDATE aimed squarely at B's row must affect zero rows.

        This is deliberately written as a blind `WHERE org_id = <B>` rather than by
        primary key: it is the shape a forgotten tenant predicate actually takes, and
        without `USING` on the policy it would succeed silently and report a rowcount.
        """
        org_a, org_b = two_orgs

        await _scope(conn, org_a)
        result = await conn.execute(
            text("UPDATE documents SET title = 'tampered' WHERE org_id = :other"),
            {"other": org_b},
        )
        assert result.rowcount == 0

        await _scope(conn, org_b)
        titles = (await conn.execute(text("SELECT title FROM documents"))).scalars().all()
        assert titles == ["beta"], "org B's row was modified from a session scoped to org A"

    async def test_cross_tenant_delete_touches_nothing(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """An unqualified DELETE must remove only the caller's own rows.

        No WHERE clause at all — the worst realistic accident. Under a correct policy it
        deletes exactly org A's row and leaves org B untouched.
        """
        org_a, org_b = two_orgs

        await _scope(conn, org_a)
        await conn.execute(text("DELETE FROM chunks"))
        await conn.execute(text("DELETE FROM document_acl"))
        deleted = await conn.execute(text("DELETE FROM documents"))
        assert deleted.rowcount == 1

        await _scope(conn, org_b)
        survivors = (await conn.execute(text("SELECT count(*) FROM documents"))).scalar_one()
        assert survivors == 1, "a DELETE scoped to org A removed org B's rows"

        await conn.rollback()

    async def test_join_cannot_widen_visibility(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """A join must not become a side channel.

        Policies apply per relation, so each side of the join is filtered independently.
        The risk is a query shaped to make the *other* side leak — here chunks joined to
        documents across the whole table. Scoped to A it must yield only A's pairing, and
        the same query scoped to B only B's.
        """
        org_a, org_b = two_orgs
        join_sql = text(
            "SELECT d.org_id AS doc_org, c.org_id AS chunk_org "
            "FROM documents d JOIN chunks c ON c.document_id = d.id"
        )

        await _scope(conn, org_a)
        rows_a = [tuple(row) for row in (await conn.execute(join_sql)).all()]
        assert rows_a == [(org_a, org_a)]

        await _scope(conn, org_b)
        rows_b = [tuple(row) for row in (await conn.execute(join_sql)).all()]
        assert rows_b == [(org_b, org_b)]

    async def test_cross_join_of_every_org_scoped_table_stays_within_the_tenant(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """A cartesian product over three protected tables must not surface a foreign id.

        Written without any join predicate on purpose: if a single one of the three
        policies is inert, a foreign `org_id` appears in the output.
        """
        org_a, _ = two_orgs

        await _scope(conn, org_a)
        result = await conn.execute(
            text(
                "SELECT DISTINCT d.org_id, c.org_id, a.org_id "
                "FROM documents d, chunks c, document_acl a"
            )
        )
        rows = [tuple(row) for row in result.all()]
        assert rows == [(org_a, org_a, org_a)]


class TestRlsCoverage:
    """Coverage asserted by enumeration, so a new table cannot slip through untested."""

    async def test_every_public_table_is_accounted_for(self, conn: AsyncConnection) -> None:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r'"
                )
            )
        ).all()

        unprotected = {
            name
            for name, enabled, forced in rows
            if not (enabled and forced)
            and name not in RLS_PENDING_BACKFILL
            and name not in RLS_EXEMPT_NO_TENANT
        }
        assert not unprotected, (
            f"tables with neither RLS nor an explicit exemption: {sorted(unprotected)}. "
            "Add the policy, or add the name to RLS_PENDING_BACKFILL with a migration to "
            "fix it."
        )

    @pytest.mark.parametrize("name", sorted(RLS_PENDING_BACKFILL | RLS_EXEMPT_NO_TENANT))
    async def test_exemption_lists_do_not_go_stale(self, conn: AsyncConnection, name: str) -> None:
        """An exemption for a table that no longer exists is a lie the suite keeps telling."""
        exists = (
            await conn.execute(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{name}"}
            )
        ).scalar_one()
        assert exists, f"{name} is exempted from RLS but no longer exists — drop the entry"

    @pytest.mark.parametrize("name", sorted(RLS_PENDING_BACKFILL))
    async def test_pending_tables_really_do_carry_a_tenant_column(
        self, conn: AsyncConnection, name: str
    ) -> None:
        """Keeps the two lists honest: a pending table must be one that *could* be scoped."""
        has_org = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t AND column_name = 'org_id'"
                ),
                {"t": name},
            )
        ).scalar_one()
        assert has_org == 1, f"{name} has no org_id — it belongs in RLS_EXEMPT_NO_TENANT"


class TestAuthSchemaBoundary:
    """The `auth` schema is org-less, so privilege — not RLS — is what contains it."""

    @pytest.mark.parametrize("table", AUTH_TABLES)
    async def test_application_role_cannot_read_auth_tables(
        self, conn: AsyncConnection, table: str
    ) -> None:
        with pytest.raises(DBAPIError) as excinfo:
            await conn.execute(text(f"SELECT count(*) FROM auth.{table}"))  # noqa: S608
        assert "permission denied" in str(excinfo.value).lower()
        await conn.rollback()

    async def test_application_role_cannot_assume_the_auth_role(
        self, conn: AsyncConnection
    ) -> None:
        """`SET ROLE` would make the definer indirection pointless.

        If `jutsu_app` were a member of `jutsu_auth`, any statement on any pooled
        connection could assume it and read every tenant's identities. Membership is
        withheld precisely so that the boundary is a privilege rather than a habit.
        """
        with pytest.raises(DBAPIError) as excinfo:
            await conn.execute(text("SET ROLE jutsu_auth"))
        assert "permission denied" in str(excinfo.value).lower()
        await conn.rollback()

    async def test_resolvers_are_executable_and_return_nothing_for_unknown_input(
        self, conn: AsyncConnection
    ) -> None:
        """The other half of the boundary: it must still be usable.

        Ownership of the tables has to follow the schema — a SECURITY DEFINER function
        runs as its own owner, so if the migration owner kept the tables these calls
        would raise "permission denied" from inside the function.
        """
        session_rows = (
            await conn.execute(text("SELECT * FROM auth.resolve_session(sha256('absent'))"))
        ).all()
        assert session_rows == []

        jutsu_rows = (
            await conn.execute(text("SELECT * FROM auth.resolve_jutsu_id('JUTSU-EMP-ZZZZZZZZ')"))
        ).all()
        assert jutsu_rows == []


class TestAuditImmutability:
    async def test_audit_rows_can_be_written_but_not_altered(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Immutability is a grant, not a docstring.

        `audit_log` was described as an immutable trail from the day it shipped while the
        application role held UPDATE and DELETE on it. 0002 revokes both.
        """
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        await conn.execute(
            text(
                "INSERT INTO audit_log (org_id, actor_id, action, resource_type, resource_id) "
                "VALUES (:org, 'actor-1', 'org.created', 'org', :rid)"
            ),
            {"org": org_a, "rid": str(org_a)},
        )

        for statement in (
            "UPDATE audit_log SET action = 'tampered'",
            "DELETE FROM audit_log",
        ):
            with pytest.raises(DBAPIError) as excinfo:
                await conn.execute(text(statement))
            assert "permission denied" in str(excinfo.value).lower()
            await conn.rollback()

    async def test_role_catalogue_is_read_only_to_the_application(
        self, conn: AsyncConnection
    ) -> None:
        """A compromised request path must not be able to mint itself a role."""
        with pytest.raises(DBAPIError) as excinfo:
            await conn.execute(
                text(
                    "INSERT INTO roles (key, name, rank, description) "
                    "VALUES ('godmode', 'God', 999, 'x')"
                )
            )
        assert "permission denied" in str(excinfo.value).lower()
        await conn.rollback()


class TestJutsuIdLedger:
    async def test_allocation_is_collision_safe_without_raising(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """A taken id yields NULL, never an IntegrityError.

        The distinction matters: an exception would poison the enclosing registration
        transaction, aborting every statement after it. Returning no row is a clean
        signal to try another candidate.
        """
        org_a, org_b = two_orgs
        candidate = "JUTSU-EMP-4P9K2MZR"

        first = (
            await conn.execute(
                text("SELECT auth.reserve_jutsu_id(:jid, :org, 'EMP')"),
                {"jid": candidate, "org": org_a},
            )
        ).scalar_one()
        assert first == candidate

        # Same id, different org — global uniqueness must still refuse it.
        second = (
            await conn.execute(
                text("SELECT auth.reserve_jutsu_id(:jid, :org, 'EMP')"),
                {"jid": candidate, "org": org_b},
            )
        ).scalar_one()
        assert second is None

    async def test_a_user_cannot_carry_an_unallocated_jutsu_id(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The composite FK is what makes a hardcoded JUTSU ID impossible.

        Without it, seeding a demo user with a plausible-looking id would succeed — and
        the brief forbids exactly that.
        """
        org_a, _ = two_orgs
        await _scope(conn, org_a)

        with pytest.raises(DBAPIError) as excinfo:
            await conn.execute(
                text(
                    "INSERT INTO users (id, org_id, email, jutsu_id) "
                    "VALUES (:id, :org, 'someone@example.com', 'JUTSU-EMP-NEVERMDE')"
                ),
                {"id": uuid.uuid4(), "org": org_a},
            )
        message = str(excinfo.value).lower()
        assert "foreign key" in message or "violates" in message
        await conn.rollback()


class TestAclPrincipalIsNotTheJutsuId:
    async def test_a_pilot_user_without_external_id_matches_no_acl_grant(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The invariant that made `external_id` nullable in the first place.

        **Migration 0008 moved the mechanism.** `document_acl.principal_id` no longer
        matches `users.external_id`; it matches a namespaced provider subject held in
        `source_identities` (ADR 0010). `external_id` was singular and one person is
        simultaneously a Google `sub`, a Slack member id and an Atlassian `accountId`.

        Both halves are asserted below, and the second is the one that matters now: the
        old join must still find nothing, *and* a user with no source identity must hold
        no principal. If the JUTSU ID were ever written into either column as a
        convenience, this test would start passing for the wrong reason — the grant would
        match a JUTSU-shaped principal and the caller would gain visibility nobody gave
        them.
        """
        org_a, _ = two_orgs
        await _scope(conn, org_a)

        user_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO users (id, org_id, email, status) "
                "VALUES (:id, :org, 'pilot@example.com', 'invited')"
            ),
            {"id": user_id, "org": org_a},
        )

        external_id = (
            await conn.execute(
                text("SELECT external_id FROM users WHERE id = :id"), {"id": user_id}
            )
        ).scalar_one()
        assert external_id is None

        matched = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM document_acl a "
                    "JOIN users u ON u.external_id = a.principal_id "
                    "WHERE u.id = :id"
                ),
                {"id": user_id},
            )
        ).scalar_one()
        assert matched == 0

        # The mechanism that actually gates access now. A user with no source identity
        # holds no ACL principal, so every predicate in the §12 filter is false.
        principals = (
            await conn.execute(
                text("SELECT count(*) FROM source_identities WHERE user_id = :id AND is_active"),
                {"id": user_id},
            )
        ).scalar_one()
        assert principals == 0, "a pilot user must hold no ACL principal"

        await conn.rollback()
