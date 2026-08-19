"""Row-level security.

An early down-payment on the adversarial ACL suite of §17. These cover tenant isolation
at the *database* layer; the document-grant cases (§17 tests 1-4) land in S7 with the
filtered search.

The invariant under test: a session scoped to org A can observe nothing about org B —
not its rows, and not its row *counts*. §17.6 treats a count leak as a leak.
"""

from __future__ import annotations

import uuid

import pytest
from jutsu_db import RLS_TABLES
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection


async def _scope(conn: AsyncConnection, org_id: uuid.UUID) -> None:
    await conn.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )


class TestConnectingRole:
    async def test_app_role_cannot_bypass_rls(self, conn: AsyncConnection) -> None:
        """The role the application connects as must not be able to ignore the policies.

        This is the test that would have caught the real failure found in S1: the
        bootstrap role created by the Postgres image is a SUPERUSER, superusers bypass
        row-level security unconditionally, and `FORCE ROW LEVEL SECURITY` does not
        change that — FORCE only covers the table owner. Connecting as that role left
        every policy inert while every other isolation assertion still passed.
        """
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        assert row.rolsuper is False, "connected as a SUPERUSER — RLS is bypassed entirely"
        assert row.rolbypassrls is False, "connected role has BYPASSRLS — RLS is bypassed"


class TestPolicyIsInstalled:
    @pytest.mark.parametrize("table", RLS_TABLES)
    async def test_rls_is_enabled_and_forced(self, conn: AsyncConnection, table: str) -> None:
        """FORCE is the half that is easy to omit.

        Without it the policy is skipped for the table owner — and dev connects as the
        owner — so every isolation test below would pass against an unenforced policy
        and the leak would only appear in production under a different role.
        """
        row = (
            await conn.execute(
                text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
                {"t": table},
            )
        ).one()
        assert row.relrowsecurity is True, f"{table}: RLS not enabled"
        assert row.relforcerowsecurity is True, f"{table}: RLS not FORCED"

    @pytest.mark.parametrize("table", RLS_TABLES)
    async def test_policy_exists(self, conn: AsyncConnection, table: str) -> None:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM pg_policies WHERE tablename = :t"), {"t": table}
            )
        ).scalar_one()
        assert count == 1, f"{table}: expected exactly one policy, found {count}"


class TestCrossOrgIsolation:
    async def test_scoped_session_sees_only_its_own_documents(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        org_a, org_b = two_orgs

        await _scope(conn, org_a)
        a_rows = (await conn.execute(text("SELECT org_id FROM documents"))).scalars().all()
        assert a_rows == [org_a]

        await _scope(conn, org_b)
        b_rows = (await conn.execute(text("SELECT org_id FROM documents"))).scalars().all()
        assert b_rows == [org_b]

    @pytest.mark.parametrize("table", RLS_TABLES)
    async def test_counts_do_not_leak(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID], table: str
    ) -> None:
        """§17.6 — two callers with disjoint access get independent counts.

        A count that reflects rows the caller cannot read is the same disclosure as
        returning them, and it is the leak that post-filtering in Python produces.
        """
        org_a, org_b = two_orgs

        await _scope(conn, org_a)
        count_a = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
        await _scope(conn, org_b)
        count_b = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608

        assert count_a == 1
        assert count_b == 1

    async def test_unscoped_session_sees_nothing(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Failing closed is the point.

        `current_setting(..., true)` returns NULL when unset, so the predicate is NULL
        and no row qualifies. Forgetting to scope a session yields an empty result, not
        every tenant's data.
        """
        await conn.rollback()  # drop any SET LOCAL from a previous transaction
        count = (await conn.execute(text("SELECT count(*) FROM documents"))).scalar_one()
        assert count == 0

    async def test_cannot_insert_into_another_org(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """WITH CHECK stops a scoped session writing rows attributed to another org."""
        org_a, org_b = two_orgs
        await _scope(conn, org_a)

        source_id = (
            await conn.execute(text("SELECT id FROM sources WHERE org_id = :org"), {"org": org_b})
        ).scalar_one()

        with pytest.raises(DBAPIError):
            await conn.execute(
                text(
                    "INSERT INTO documents (id, org_id, source_id, external_id, title, "
                    "content_hash, acl_hash, body_original, body_masked, created_at) "
                    "VALUES (:id, :org, :src, 'x', 't', 'h', 'a', 'b', 'b', now())"
                ),
                {"id": uuid.uuid4(), "org": org_b, "src": source_id},
            )
        await conn.rollback()


class TestStructuralIntegrity:
    async def test_chunk_cannot_reference_a_foreign_org_document(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The composite FK from ADR 0002 is what keeps the denormalised org_id honest.

        Without it, a bug could attach org A's chunk to org B's document and RLS would
        happily serve it to org A.
        """
        org_a, org_b = two_orgs
        await _scope(conn, org_b)
        foreign_doc = (
            await conn.execute(text("SELECT id FROM documents WHERE org_id = :org"), {"org": org_b})
        ).scalar_one()

        await conn.rollback()
        await _scope(conn, org_a)
        with pytest.raises(DBAPIError):
            await conn.execute(
                text(
                    "INSERT INTO chunks (id, document_id, org_id, ordinal, text, "
                    "char_start, char_end, token_count) "
                    "VALUES (:id, :doc, :org, 0, 't', 0, 1, 1)"
                ),
                {"id": uuid.uuid4(), "doc": foreign_doc, "org": org_a},
            )
        await conn.rollback()

    async def test_acl_permission_is_read_only(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """§4.8 — no write scope is ever requested, so the schema refuses to store one."""
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        doc = (
            await conn.execute(text("SELECT id FROM documents WHERE org_id = :org"), {"org": org_a})
        ).scalar_one()

        with pytest.raises(DBAPIError):
            await conn.execute(
                text(
                    "INSERT INTO document_acl (document_id, principal_type, principal_id, "
                    "org_id, permission) VALUES (:doc, 'user', 'u9', :org, 'write')"
                ),
                {"doc": doc, "org": org_a},
            )
        await conn.rollback()
