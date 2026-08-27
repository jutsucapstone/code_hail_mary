"""Eager ACL principal resolution on the authenticated caller (ADR 0010).

The database half lives in `packages/db/tests/test_source_identities.py`. This is the
half that matters at the request boundary: what `Principal` actually carries, and that it
cannot be widened from a call site.

Two properties are load-bearing and both are asserted rather than described:

  * **Eager, never cached.** §17 test 3 requires a group removal to take effect on the
    next query with no cache flush. The same must hold for revoking a source identity, so
    resolution reads the database on every request and the tests revoke between calls.
  * **The organisation is never an argument.** `scoped_acl_principals` takes a user id and
    nothing else; the tenant comes from the GUC that `scoped_role` set from the
    *session-derived* org id. There is no parameter a request could populate.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from jutsu_api.auth_service import scoped_acl_principals
from jutsu_api.security import Principal
from jutsu_core.rbac import Permission, Role
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def scope(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )


async def make_org_with_user(session: AsyncSession, label: str) -> tuple[uuid.UUID, uuid.UUID]:
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    await scope(session, org_id)
    await session.execute(
        text("INSERT INTO orgs (id, name) VALUES (:id, :name)"), {"id": org_id, "name": label}
    )
    await session.execute(
        text("INSERT INTO users (id, org_id, email, status) VALUES (:id, :org, :email, 'active')"),
        {"id": user_id, "org": org_id, "email": f"{label}@example.com"},
    )
    return org_id, user_id


async def link(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    system: str,
    subject: str,
    *,
    active: bool = True,
) -> None:
    await session.execute(
        text(
            "INSERT INTO source_identities (org_id, user_id, source_system, subject, is_active) "
            "VALUES (:org, :user, CAST(:system AS source_system), :subject, :active)"
        ),
        {"org": org_id, "user": user_id, "system": system, "subject": subject, "active": active},
    )


async def add_group(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, group: str
) -> None:
    await session.execute(
        text("INSERT INTO user_groups (user_id, org_id, group_external_id) VALUES (:u, :o, :g)"),
        {"u": user_id, "o": org_id, "g": group},
    )


class TestResolution:
    async def test_a_single_identity_resolves_namespaced(self, db_session: AsyncSession) -> None:
        org, user = await make_org_with_user(db_session, "alpha")
        await link(db_session, org, user, "slack", "U01ABC")

        principals, groups = await scoped_acl_principals(db_session, user_id=user)
        assert principals == frozenset({"slack:U01ABC"})
        assert groups == frozenset()

    async def test_multiple_identities_all_resolve(self, db_session: AsyncSession) -> None:
        org, user = await make_org_with_user(db_session, "alpha")
        await link(db_session, org, user, "slack", "U01ABC")
        await link(db_session, org, user, "m365", "00000000-0000-0000-0000-000000000001")
        await link(db_session, org, user, "local", "ada@example.com")

        principals, _ = await scoped_acl_principals(db_session, user_id=user)
        assert principals == frozenset(
            {
                "slack:U01ABC",
                "m365:00000000-0000-0000-0000-000000000001",
                "local:ada@example.com",
            }
        )

    async def test_the_local_namespace_carries_an_address(self, db_session: AsyncSession) -> None:
        """S3's corpus subject really is an address, and it is labelled as such."""
        org, user = await make_org_with_user(db_session, "alpha")
        await link(db_session, org, user, "local", "phillip.allen@example.com")

        principals, _ = await scoped_acl_principals(db_session, user_id=user)
        assert principals == frozenset({"local:phillip.allen@example.com"})

    async def test_identical_subjects_in_two_providers_do_not_collide(
        self, db_session: AsyncSession
    ) -> None:
        org, user = await make_org_with_user(db_session, "alpha")
        await link(db_session, org, user, "slack", "12345")
        await link(db_session, org, user, "github", "12345")

        principals, _ = await scoped_acl_principals(db_session, user_id=user)
        assert principals == frozenset({"slack:12345", "github:12345"})
        assert len(principals) == 2, "the namespace collapsed two distinct identities"

    async def test_groups_resolve_alongside_identities(self, db_session: AsyncSession) -> None:
        org, user = await make_org_with_user(db_session, "alpha")
        await link(db_session, org, user, "slack", "U01ABC")
        await add_group(db_session, org, user, "slack:S-ENGINEERING")

        principals, groups = await scoped_acl_principals(db_session, user_id=user)
        assert principals == frozenset({"slack:U01ABC"})
        assert groups == frozenset({"slack:S-ENGINEERING"})


class TestFailClosed:
    async def test_a_user_with_no_identity_holds_no_principals(
        self, db_session: AsyncSession
    ) -> None:
        """The default state of every invited user, and it must see nothing."""
        _org, user = await make_org_with_user(db_session, "alpha")
        principals, groups = await scoped_acl_principals(db_session, user_id=user)
        assert principals == frozenset()
        assert groups == frozenset()

    async def test_an_inactive_identity_is_not_resolved(self, db_session: AsyncSession) -> None:
        org, user = await make_org_with_user(db_session, "alpha")
        await link(db_session, org, user, "slack", "U01ABC", active=False)

        principals, _ = await scoped_acl_principals(db_session, user_id=user)
        assert principals == frozenset()

    async def test_a_principal_with_no_identities_is_the_default(self) -> None:
        """Constructing a `Principal` without principals must not open anything."""
        principal = Principal(
            session_id=uuid.uuid4(),
            identity_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            role=Role.OWNER,
            permissions=frozenset(Permission),
        )
        assert principal.acl_principals == frozenset()
        assert principal.acl_groups == frozenset()


class TestRevocationIsImmediate:
    async def test_revoking_an_identity_affects_the_next_resolution(
        self, db_session: AsyncSession
    ) -> None:
        """No cache to flush — the second call reads the database again."""
        org, user = await make_org_with_user(db_session, "alpha")
        await link(db_session, org, user, "slack", "U01ABC")
        assert (await scoped_acl_principals(db_session, user_id=user))[0] == frozenset(
            {"slack:U01ABC"}
        )

        await db_session.execute(
            text(
                "UPDATE source_identities SET is_active = false, revoked_at = now() "
                "WHERE user_id = :u"
            ),
            {"u": user},
        )
        assert (await scoped_acl_principals(db_session, user_id=user))[0] == frozenset()

    async def test_removing_a_group_affects_the_next_resolution(
        self, db_session: AsyncSession
    ) -> None:
        """§17 test 3, at the layer that builds the caller's group list."""
        org, user = await make_org_with_user(db_session, "alpha")
        await add_group(db_session, org, user, "slack:S-ENGINEERING")
        assert (await scoped_acl_principals(db_session, user_id=user))[1] == frozenset(
            {"slack:S-ENGINEERING"}
        )

        await db_session.execute(text("DELETE FROM user_groups WHERE user_id = :u"), {"u": user})
        assert (await scoped_acl_principals(db_session, user_id=user))[1] == frozenset()


class TestTenantScope:
    def test_the_function_takes_no_organisation(self) -> None:
        """The scope cannot come from a call site, so a request cannot widen it.

        Asserted on the signature rather than described in a docstring: an `org_id`
        parameter appearing here later would be the exact regression this prevents.
        """
        parameters = set(inspect.signature(scoped_acl_principals).parameters)
        assert parameters == {"session", "user_id"}
        assert "org_id" not in parameters

    async def test_resolution_is_scoped_by_the_guc_not_the_argument(
        self, db_session: AsyncSession
    ) -> None:
        """Adversarial: the same user id, read under another tenant's scope."""
        org_a, user_a = await make_org_with_user(db_session, "alpha")
        await link(db_session, org_a, user_a, "slack", "U01ABC")
        org_b, _user_b = await make_org_with_user(db_session, "beta")

        await scope(db_session, org_b)
        principals, _ = await scoped_acl_principals(db_session, user_id=user_a)
        assert principals == frozenset(), "another tenant's identity resolved"

    async def test_an_unscoped_session_resolves_nothing(self, db_session: AsyncSession) -> None:
        org, user = await make_org_with_user(db_session, "alpha")
        await link(db_session, org, user, "slack", "U01ABC")

        await db_session.execute(text("SELECT set_config('app.current_org_id', '', true)"))
        principals, groups = await scoped_acl_principals(db_session, user_id=user)
        assert principals == frozenset()
        assert groups == frozenset()

    async def test_groups_do_not_cross_organisations(self, db_session: AsyncSession) -> None:
        org_a, user_a = await make_org_with_user(db_session, "alpha")
        await add_group(db_session, org_a, user_a, "slack:S-ALPHA")
        org_b, _user_b = await make_org_with_user(db_session, "beta")

        await scope(db_session, org_b)
        _, groups = await scoped_acl_principals(db_session, user_id=user_a)
        assert groups == frozenset()


class TestPrincipalCarriesNoSecret:
    async def test_the_principal_repr_holds_no_credential(self, db_session: AsyncSession) -> None:
        """ACL principals are identifiers, not secrets — but the object is logged, so it
        is worth pinning that nothing else crept onto it."""
        org, user = await make_org_with_user(db_session, "alpha")
        await link(db_session, org, user, "slack", "U01ABC")
        principals, groups = await scoped_acl_principals(db_session, user_id=user)

        principal = Principal(
            session_id=uuid.uuid4(),
            identity_id=uuid.uuid4(),
            user_id=user,
            org_id=org,
            role=Role.MEMBER,
            permissions=frozenset(),
            acl_principals=principals,
            acl_groups=groups,
        )
        rendered = repr(principal)
        assert "slack:U01ABC" in rendered
        for forbidden in ("token", "csrf", "password", "secret"):
            assert forbidden not in rendered.lower()


@pytest.mark.parametrize(
    ("system", "subject"),
    [
        ("local", "ada@example.com"),
        ("gmail", "117012345678901234567"),
        ("m365", "00000000-0000-0000-0000-000000000002"),
        ("slack", "U01ABCDEF"),
        ("github", "12345678"),
        ("jira", "5b10ac8d82e05b22cc7d4ef5"),
        ("confluence", "5b10ac8d82e05b22cc7d4ef5"),
    ],
)
async def test_every_source_system_namespaces_its_subject(
    db_session: AsyncSession, system: str, subject: str
) -> None:
    """Every connector in `SourceSystem`, including the two Atlassian products that share
    an `accountId` namespace and must still resolve as distinct principals."""
    org, user = await make_org_with_user(db_session, f"org-{system}")
    await link(db_session, org, user, system, subject)

    principals, _ = await scoped_acl_principals(db_session, user_id=user)
    assert principals == frozenset({f"{system}:{subject}"})
