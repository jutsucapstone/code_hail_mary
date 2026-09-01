"""The source identity lifecycle, and the refusals that make it safe (ADR 0010).

S6.5 built the table and resolved principals from it. Nothing wrote to it, which ADR 0010
recorded as the open limitation: *"every real user resolves to an empty principal set and
sees nothing — the correct default, and not a working authorization system."* This suite
covers what closes that gap, and it is written adversarially because **every row in
`source_identities` is a grant of document access**.

Two halves, tested two ways on purpose:

  * **Automatic linking** goes through the real HTTP flows. A test that called
    `link_verified_email` directly would prove the function works and say nothing about
    whether registration calls it — and "nothing calls it" is precisely the defect this
    slice exists to fix. So these tests register and accept invitations over the wire.
  * **Administrative linking** is tested at the service layer, where the four refusals
    live and can be provoked one at a time. The route guards are asserted separately,
    against the permission catalogue rather than against a docstring.

The most important test in this file is
`test_an_administrator_cannot_link_a_subject_to_themselves`. §17 keeps roles and ACLs
apart — nothing in `Permission` may confer a document read — and a self-link would turn a
role into a way to hand yourself somebody else's documents.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.auth_service import scoped_acl_principals
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.identities import (
    LINKED_BY_ADMIN,
    LINKED_BY_VERIFIED_EMAIL,
    link_identity,
    link_verified_email,
    list_identities,
    revoke_all_for_user,
    revoke_identity,
)
from jutsu_api.main import create_app
from jutsu_api.routers import identities as identities_router
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER, declaration_of
from jutsu_core import SourceSystem
from jutsu_core.errors import Conflict, NotFound, PermissionDenied
from jutsu_core.rbac import ROLE_PERMISSIONS, Permission, Role
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

REGISTRATION = {
    "full_name": "Ada Lovelace",
    "work_email": "ada@example.com",
    "company_name": "Example Analytical",
    "company_domain": "example.com",
    "job_title": "Head of Engineering",
    "org_size": "51-200",
    "terms_accepted": True,
}


@pytest.fixture
async def client(
    db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as http:
        yield http


def csrf_headers(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


async def register_owner(client: AsyncClient, mailbox: RecordingEmailSender) -> None:
    await client.post("/v1/orgs/register", json=REGISTRATION)
    delivered = mailbox.last.secrets
    response = await client.post(
        "/v1/orgs/register/verify",
        json={"token": delivered["token"], "code": delivered["code"]},
    )
    assert response.status_code == 200, response.text


async def invite_and_accept(
    client: AsyncClient,
    mailbox: RecordingEmailSender,
    *,
    email: str = "charles@example.com",
    full_name: str = "Charles Babbage",
    role: str = "member",
) -> None:
    invited = await client.post(
        "/v1/employees/invitations",
        json={"email": email, "role": role},
        headers=csrf_headers(client),
    )
    assert invited.status_code in (200, 201, 202), invited.text
    token = mailbox.last.secrets["token"]
    accepted = await client.post(
        "/v1/invitations/accept", json={"token": token, "full_name": full_name}
    )
    assert accepted.status_code == 200, accepted.text


async def identity_rows(inspector: AsyncSession, subject: str) -> list[Any]:
    """Read through the privileged connection: RLS hides these from an unscoped read.

    An assertion of the form "no identity was created" is vacuous on `db_session`, which
    is subject to row-level security — it would return nothing whether or not the row
    exists. The same trap `inspector` was added for in `conftest`.
    """
    return list(
        (
            await inspector.execute(
                text(
                    "SELECT si.id, si.subject, si.source_system, si.is_active, si.linked_by, "
                    "si.user_id, si.org_id, u.email FROM source_identities si "
                    "JOIN users u ON u.id = si.user_id WHERE si.subject = :s"
                ),
                {"s": subject},
            )
        ).all()
    )


# --------------------------------------------------------------------------------------
# Automatic linking, over the wire.
# --------------------------------------------------------------------------------------


class TestRegistrationLinksTheVerifiedAddress:
    async def test_completing_a_registration_creates_a_local_identity(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        """The gap ADR 0010 left open, closed at the point mailbox control is proven."""
        await register_owner(client, mailbox)

        rows = await identity_rows(inspector, "ada@example.com")
        assert len(rows) == 1, "registration must link exactly one local identity"
        assert rows[0].source_system == "local"
        assert rows[0].is_active is True
        assert rows[0].linked_by == LINKED_BY_VERIFIED_EMAIL

    async def test_the_subject_is_the_address_the_code_was_sent_to(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        """Not merely equal to a request field — equal to `users.email`, which the OTP proved."""
        await register_owner(client, mailbox)

        rows = await identity_rows(inspector, "ada@example.com")
        assert rows[0].subject == rows[0].email

    async def test_staging_alone_links_nothing(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        """Fails closed: an unredeemed code is not proof, so it grants nothing."""
        await client.post("/v1/orgs/register", json=REGISTRATION)

        assert await identity_rows(inspector, "ada@example.com") == []

    async def test_the_new_owner_resolves_that_principal(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """End to end: the row is not merely present, it reaches `Principal.acl_principals`."""
        await register_owner(client, mailbox)
        me = (await client.get("/v1/me")).json()
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
        )
        principals, _ = await scoped_acl_principals(db_session, user_id=uuid.UUID(me["user_id"]))

        assert principals == frozenset({"local:ada@example.com"})

    async def test_the_self_route_shows_it(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)

        page = await client.get("/v1/me/identities")

        assert page.status_code == 200, page.text
        items = page.json()["items"]
        assert [(i["source_system"], i["subject"], i["is_active"]) for i in items] == [
            ("local", "ada@example.com", True)
        ]
        assert items[0]["linked_by"] == LINKED_BY_VERIFIED_EMAIL


class TestInvitationAcceptanceLinksTheInvitedAddress:
    async def test_accepting_creates_a_local_identity(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox)

        rows = await identity_rows(inspector, "charles@example.com")
        assert len(rows) == 1
        assert rows[0].linked_by == LINKED_BY_VERIFIED_EMAIL
        assert rows[0].is_active is True

    async def test_the_subject_comes_from_the_invitation_not_the_request(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        """The request body carries a display name and nothing else.

        The invitee submits `full_name` freely. If that string could reach the subject,
        accepting an invitation would be a way to claim any principal in the tenant. Here
        it is an address belonging to somebody else, and it must land nowhere.
        """
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, full_name="ada@example.com")

        assert len(await identity_rows(inspector, "charles@example.com")) == 1
        owner_rows = await identity_rows(inspector, "ada@example.com")
        assert len(owner_rows) == 1, "a display name minted a second claim on the owner"
        assert owner_rows[0].email == "ada@example.com", "a display name claimed a principal"

    async def test_an_unaccepted_invitation_links_nothing(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        await client.post(
            "/v1/employees/invitations",
            json={"email": "charles@example.com", "role": "member"},
            headers=csrf_headers(client),
        )

        assert await identity_rows(inspector, "charles@example.com") == []

    async def test_the_invited_member_can_read_their_own(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """`integration:self_manage` is held by every role, including a bare Member.

        §31: a transparency surface an ordinary member cannot open is not a transparency
        surface. Accepting an invitation also opens a session, so this client is now the
        invitee rather than the owner.
        """
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox)

        page = await client.get("/v1/me/identities")

        assert page.status_code == 200, page.text
        assert [i["subject"] for i in page.json()["items"]] == ["charles@example.com"]

    async def test_a_member_cannot_read_another_employees_identities(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        owner_id = (
            await inspector.execute(text("SELECT id FROM users WHERE email = 'ada@example.com'"))
        ).scalar_one()
        await invite_and_accept(client, mailbox)

        response = await client.get(f"/v1/employees/{owner_id}/identities")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    async def test_a_member_cannot_link_a_subject(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        """The outer gate. `integration:connect` is held by neither a Member nor HR."""
        await register_owner(client, mailbox)
        owner_id = (
            await inspector.execute(text("SELECT id FROM users WHERE email = 'ada@example.com'"))
        ).scalar_one()
        await invite_and_accept(client, mailbox)

        response = await client.post(
            f"/v1/employees/{owner_id}/identities",
            json={"source_system": "slack", "subject": "U01STOLEN"},
            headers=csrf_headers(client),
        )

        assert response.status_code == 403
        assert await identity_rows(inspector, "U01STOLEN") == []


# --------------------------------------------------------------------------------------
# The service layer: seeding, then the refusals.
# --------------------------------------------------------------------------------------


async def scope(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )


async def make_org(session: AsyncSession, label: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    await scope(session, org_id)
    await session.execute(
        text("INSERT INTO orgs (id, name) VALUES (:id, :name)"), {"id": org_id, "name": label}
    )
    return org_id


async def make_user(session: AsyncSession, org_id: uuid.UUID, label: str, role: Role) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, org_id, email, status) VALUES (:id, :org, :email, 'active')"),
        {"id": user_id, "org": org_id, "email": f"{label}@example.com"},
    )
    await session.execute(
        text("INSERT INTO user_roles (user_id, org_id, role_key) VALUES (:u, :o, :r)"),
        {"u": user_id, "o": org_id, "r": role.value},
    )
    return user_id


async def audit_rows(session: AsyncSession, action: str) -> list[Any]:
    return list(
        (
            await session.execute(
                text(
                    "SELECT actor_id, actor_type, resource_type, resource_id, outcome "
                    "FROM audit_log WHERE action = :a ORDER BY id"
                ),
                {"a": action},
            )
        ).all()
    )


class TestIdempotentAutomaticLinking:
    async def test_linking_the_same_address_twice_creates_one_row(
        self, db_session: AsyncSession
    ) -> None:
        """§4.14. A retried registration must not double-write an ACL principal."""
        org = await make_org(db_session, "alpha")
        user = await make_user(db_session, org, "ada", Role.OWNER)

        await link_verified_email(db_session, org_id=org, user_id=user, verified_email="a@x.test")
        await link_verified_email(db_session, org_id=org, user_id=user, verified_email="a@x.test")

        assert len(await list_identities(db_session, user_id=user)) == 1

    async def test_an_address_held_by_somebody_else_links_nothing(
        self, db_session: AsyncSession
    ) -> None:
        """Fails closed rather than moving access between people.

        One subject is one person per tenant. The second user ends with no principal,
        which is the safe direction: nobody gains anything they had not proven.
        """
        org = await make_org(db_session, "alpha")
        first = await make_user(db_session, org, "ada", Role.OWNER)
        second = await make_user(db_session, org, "grace", Role.MEMBER)

        await link_verified_email(db_session, org_id=org, user_id=first, verified_email="a@x.test")
        await link_verified_email(db_session, org_id=org, user_id=second, verified_email="a@x.test")

        assert len(await list_identities(db_session, user_id=first)) == 1
        assert await list_identities(db_session, user_id=second) == []

    async def test_reverifying_after_a_revocation_relinks(self, db_session: AsyncSession) -> None:
        """The reconnect case: a revoked row is history, not a permanent claim.

        Before migration 0016 the `ON CONFLICT DO NOTHING` hit the revoked row and the
        employee silently regained nothing — fail-closed against the wrong row. The new
        active link lands beside the revoked one, so the trail still shows both.
        """
        org = await make_org(db_session, "alpha")
        member = await make_user(db_session, org, "grace", Role.MEMBER)
        await link_verified_email(db_session, org_id=org, user_id=member, verified_email="g@x.test")
        first = (await list_identities(db_session, user_id=member))[0]
        await revoke_identity(
            db_session,
            actor_id=member,
            actor_org_id=org,
            actor_role=Role.MEMBER,
            target_user_id=member,
            identity_id=first.id,
        )

        await link_verified_email(db_session, org_id=org, user_id=member, verified_email="g@x.test")

        rows = await list_identities(db_session, user_id=member)
        assert len(rows) == 2, "the revoked row must survive beside the fresh link"
        assert {row.is_active for row in rows} == {True, False}
        active = next(row for row in rows if row.is_active)
        assert active.id != first.id
        assert active.linked_by == LINKED_BY_VERIFIED_EMAIL
        principals, _ = await scoped_acl_principals(db_session, user_id=member)
        assert principals == frozenset({"local:g@x.test"})

    async def test_two_tenants_may_hold_the_same_address(self, db_session: AsyncSession) -> None:
        """Uniqueness is `(org_id, source_system, subject)`, never global (ADR 0010 §3)."""
        alpha = await make_org(db_session, "alpha")
        alpha_user = await make_user(db_session, alpha, "ada", Role.OWNER)
        await link_verified_email(
            db_session, org_id=alpha, user_id=alpha_user, verified_email="a@x.test"
        )

        beta = await make_org(db_session, "beta")
        beta_user = await make_user(db_session, beta, "ada", Role.OWNER)
        await link_verified_email(
            db_session, org_id=beta, user_id=beta_user, verified_email="a@x.test"
        )

        assert len(await list_identities(db_session, user_id=beta_user)) == 1


class TestCrossTenantReads:
    async def test_another_tenants_identities_read_as_empty(self, db_session: AsyncSession) -> None:
        """The read path is scoped by RLS too, not only the write paths.

        `/v1/employees/{id}/identities` has no rank check — `integration:read` is the
        ceiling — so the tenant boundary is the only thing standing between an
        administrator and another organisation's principals. It is the database that
        stands there, not a Python comparison: the row is invisible, so the page is empty
        rather than refused, and the caller learns nothing from the difference.
        """
        beta = await make_org(db_session, "beta")
        stranger = await make_user(db_session, beta, "eve", Role.MEMBER)
        await link_verified_email(
            db_session, org_id=beta, user_id=stranger, verified_email="eve@example.com"
        )
        assert len(await list_identities(db_session, user_id=stranger)) == 1

        await make_org(db_session, "alpha")

        assert await list_identities(db_session, user_id=stranger) == []
        principals, _ = await scoped_acl_principals(db_session, user_id=stranger)
        assert principals == frozenset(), "a foreign principal resolved inside another tenant"


class TestAdministrativeLinking:
    async def test_an_admin_links_a_subject_for_a_subordinate(
        self, db_session: AsyncSession
    ) -> None:
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        target = await make_user(db_session, org, "grace", Role.MEMBER)

        linked = await link_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=target,
            source_system=SourceSystem.SLACK,
            subject="U01ABCDEF",
        )

        assert linked.subject == "U01ABCDEF"
        assert linked.linked_by == LINKED_BY_ADMIN
        assert linked.is_active is True
        principals, _ = await scoped_acl_principals(db_session, user_id=target)
        assert principals == frozenset({"slack:U01ABCDEF"})

    async def test_an_administrator_cannot_link_a_subject_to_themselves(
        self, db_session: AsyncSession
    ) -> None:
        """The single most important refusal in this slice.

        §17 divides the world: roles gate features, ACLs gate data, and `rbac.py` states
        that nothing in `Permission` can grant a document read. An IT Admin who could link
        themselves to a colleague's Slack subject would read that colleague's documents
        with no ACL ever consulted — a role conferring data access, which is precisely the
        property the separation exists to deny.
        """
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)

        with pytest.raises(PermissionDenied):
            await link_identity(
                db_session,
                actor_id=admin,
                actor_org_id=org,
                actor_role=Role.IT_ADMIN,
                target_user_id=admin,
                source_system=SourceSystem.SLACK,
                subject="U01VICTIM",
            )

        assert await list_identities(db_session, user_id=admin) == []

    async def test_not_even_an_owner_may_self_link(self, db_session: AsyncSession) -> None:
        """Unconditional, and not gated on a permission.

        An Owner holds every permission there is. If the refusal were a permission check
        it would be no refusal at all for the one role that most needs it.
        """
        org = await make_org(db_session, "alpha")
        owner = await make_user(db_session, org, "ada", Role.OWNER)

        with pytest.raises(PermissionDenied):
            await link_identity(
                db_session,
                actor_id=owner,
                actor_org_id=org,
                actor_role=Role.OWNER,
                target_user_id=owner,
                source_system=SourceSystem.GMAIL,
                subject="103948",
            )

    async def test_a_peer_cannot_be_linked(self, db_session: AsyncSession) -> None:
        """`outranks` is strict. Two IT Admins are equal, so neither may act on the other."""
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        peer = await make_user(db_session, org, "ivor", Role.IT_ADMIN)

        with pytest.raises(PermissionDenied):
            await link_identity(
                db_session,
                actor_id=admin,
                actor_org_id=org,
                actor_role=Role.IT_ADMIN,
                target_user_id=peer,
                source_system=SourceSystem.SLACK,
                subject="U01PEER",
            )

    async def test_a_superior_cannot_be_linked(self, db_session: AsyncSession) -> None:
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        owner = await make_user(db_session, org, "ada", Role.OWNER)

        with pytest.raises(PermissionDenied):
            await link_identity(
                db_session,
                actor_id=admin,
                actor_org_id=org,
                actor_role=Role.IT_ADMIN,
                target_user_id=owner,
                source_system=SourceSystem.SLACK,
                subject="U01BOSS",
            )

    async def test_a_user_in_another_tenant_is_not_found(self, db_session: AsyncSession) -> None:
        """Row-level security answers this, not a Python comparison.

        `NotFound` rather than `PermissionDenied`: the caller learns nothing about whether
        the id exists somewhere they cannot see.
        """
        other = await make_org(db_session, "beta")
        stranger = await make_user(db_session, other, "eve", Role.MEMBER)
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)

        with pytest.raises(NotFound):
            await link_identity(
                db_session,
                actor_id=admin,
                actor_org_id=org,
                actor_role=Role.IT_ADMIN,
                target_user_id=stranger,
                source_system=SourceSystem.SLACK,
                subject="U01CROSS",
            )

    async def test_a_duplicate_subject_is_a_conflict_not_a_transfer(
        self, db_session: AsyncSession
    ) -> None:
        """Moving a subject between people is a transfer of access, so it must be explicit."""
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        first = await make_user(db_session, org, "grace", Role.MEMBER)
        second = await make_user(db_session, org, "alan", Role.MEMBER)
        await link_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=first,
            source_system=SourceSystem.SLACK,
            subject="U01ABCDEF",
        )

        with pytest.raises(Conflict):
            await link_identity(
                db_session,
                actor_id=admin,
                actor_org_id=org,
                actor_role=Role.IT_ADMIN,
                target_user_id=second,
                source_system=SourceSystem.SLACK,
                subject="U01ABCDEF",
            )

        principals, _ = await scoped_acl_principals(db_session, user_id=first)
        assert principals == frozenset({"slack:U01ABCDEF"}), "the original link moved"

    async def test_revoke_then_link_moves_a_subject_to_its_new_holder(
        self, db_session: AsyncSession
    ) -> None:
        """The sanctioned transfer flow, end to end.

        The Conflict above tells an administrator a transfer "must be an explicit
        revoke-then-link". Until migration 0016 the revoked row still occupied an
        unconditional unique constraint, so the second half of that instruction was a
        permanent 409 — the flow the product prescribed was impossible by schema.
        """
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        leaver = await make_user(db_session, org, "grace", Role.MEMBER)
        successor = await make_user(db_session, org, "alan", Role.MEMBER)
        linked = await link_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=leaver,
            source_system=SourceSystem.SLACK,
            subject="U01ABCDEF",
        )

        await revoke_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=leaver,
            identity_id=linked.id,
        )
        relinked = await link_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=successor,
            source_system=SourceSystem.SLACK,
            subject="U01ABCDEF",
        )

        assert relinked.is_active is True
        assert relinked.id != linked.id, "the transfer must mint a new row, not move one"
        old_principals, _ = await scoped_acl_principals(db_session, user_id=leaver)
        new_principals, _ = await scoped_acl_principals(db_session, user_id=successor)
        assert old_principals == frozenset()
        assert new_principals == frozenset({"slack:U01ABCDEF"})

        # Both acts are on the record: the transfer reads as a revoke AND a link, each
        # naming the identity row it touched.
        revoked = await audit_rows(db_session, "identity.revoked")
        linked_rows = await audit_rows(db_session, "identity.linked")
        assert [uuid.UUID(str(row.resource_id)) for row in revoked] == [linked.id]
        assert [uuid.UUID(str(row.resource_id)) for row in linked_rows] == [
            linked.id,
            relinked.id,
        ]

    async def test_a_blank_subject_is_refused(self, db_session: AsyncSession) -> None:
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        target = await make_user(db_session, org, "grace", Role.MEMBER)

        with pytest.raises(Conflict):
            await link_identity(
                db_session,
                actor_id=admin,
                actor_org_id=org,
                actor_role=Role.IT_ADMIN,
                target_user_id=target,
                source_system=SourceSystem.SLACK,
                subject="   ",
            )


class TestRevocation:
    async def test_revoking_keeps_the_row_and_stamps_it(self, db_session: AsyncSession) -> None:
        """A flag, not a delete: the audit trail must still explain what access existed."""
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        target = await make_user(db_session, org, "grace", Role.MEMBER)
        linked = await link_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=target,
            source_system=SourceSystem.SLACK,
            subject="U01ABCDEF",
        )

        await revoke_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=target,
            identity_id=linked.id,
        )

        rows = await list_identities(db_session, user_id=target)
        assert len(rows) == 1, "the row must survive revocation"
        assert rows[0].is_active is False
        assert rows[0].revoked_at is not None

    async def test_a_revoked_identity_stops_resolving_immediately(
        self, db_session: AsyncSession
    ) -> None:
        """§17 test 3, applied to identities: no cache flush, no next login, no delay.

        Resolution reads the database on every request, so a revocation takes effect on
        the caller's next one. The assertion is the pair either side of the revoke.
        """
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        target = await make_user(db_session, org, "grace", Role.MEMBER)
        linked = await link_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=target,
            source_system=SourceSystem.SLACK,
            subject="U01ABCDEF",
        )
        before, _ = await scoped_acl_principals(db_session, user_id=target)

        await revoke_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=target,
            identity_id=linked.id,
        )
        after, _ = await scoped_acl_principals(db_session, user_id=target)

        assert before == frozenset({"slack:U01ABCDEF"})
        assert after == frozenset()

    async def test_revoking_your_own_identity_is_allowed(self, db_session: AsyncSession) -> None:
        """Removing your own access is not an escalation, and refusing it would strand people."""
        org = await make_org(db_session, "alpha")
        member = await make_user(db_session, org, "grace", Role.MEMBER)
        await link_verified_email(
            db_session, org_id=org, user_id=member, verified_email="grace@example.com"
        )
        own = (await list_identities(db_session, user_id=member))[0]

        await revoke_identity(
            db_session,
            actor_id=member,
            actor_org_id=org,
            actor_role=Role.MEMBER,
            target_user_id=member,
            identity_id=own.id,
        )

        assert (await list_identities(db_session, user_id=member))[0].is_active is False

    async def test_revoking_twice_is_not_found(self, db_session: AsyncSession) -> None:
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        target = await make_user(db_session, org, "grace", Role.MEMBER)
        linked = await link_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=target,
            source_system=SourceSystem.SLACK,
            subject="U01ABCDEF",
        )
        await revoke_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=target,
            identity_id=linked.id,
        )

        with pytest.raises(NotFound):
            await revoke_identity(
                db_session,
                actor_id=admin,
                actor_org_id=org,
                actor_role=Role.IT_ADMIN,
                target_user_id=target,
                identity_id=linked.id,
            )

    async def test_another_tenants_identity_is_not_found(self, db_session: AsyncSession) -> None:
        """The `UPDATE` is scoped by RLS, so a foreign id matches no row at all."""
        beta = await make_org(db_session, "beta")
        stranger = await make_user(db_session, beta, "eve", Role.MEMBER)
        await link_verified_email(
            db_session, org_id=beta, user_id=stranger, verified_email="eve@example.com"
        )
        theirs = (await list_identities(db_session, user_id=stranger))[0]

        alpha = await make_org(db_session, "alpha")
        admin = await make_user(db_session, alpha, "ida", Role.IT_ADMIN)

        with pytest.raises(NotFound):
            await revoke_identity(
                db_session,
                actor_id=admin,
                actor_org_id=alpha,
                actor_role=Role.IT_ADMIN,
                target_user_id=stranger,
                identity_id=theirs.id,
            )

    async def test_a_peers_identity_cannot_be_revoked(self, db_session: AsyncSession) -> None:
        """Revocation carries the same rank ceiling as linking: it is acting on access."""
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        peer = await make_user(db_session, org, "ivor", Role.IT_ADMIN)
        await link_verified_email(
            db_session, org_id=org, user_id=peer, verified_email="ivor@example.com"
        )
        theirs = (await list_identities(db_session, user_id=peer))[0]

        with pytest.raises(PermissionDenied):
            await revoke_identity(
                db_session,
                actor_id=admin,
                actor_org_id=org,
                actor_role=Role.IT_ADMIN,
                target_user_id=peer,
                identity_id=theirs.id,
            )

        assert (await list_identities(db_session, user_id=peer))[0].is_active is True

    async def test_revoke_all_closes_every_active_identity(self, db_session: AsyncSession) -> None:
        """The offboarding primitive. It has no production caller yet, and is tested anyway."""
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        target = await make_user(db_session, org, "grace", Role.MEMBER)
        await link_verified_email(
            db_session, org_id=org, user_id=target, verified_email="grace@example.com"
        )
        for subject in ("U01ABCDEF", "U02ABCDEF"):
            await link_identity(
                db_session,
                actor_id=admin,
                actor_org_id=org,
                actor_role=Role.IT_ADMIN,
                target_user_id=target,
                source_system=SourceSystem.SLACK,
                subject=subject,
            )

        closed = await revoke_all_for_user(
            db_session, actor_id=admin, actor_org_id=org, target_user_id=target
        )

        assert closed == 3
        principals, _ = await scoped_acl_principals(db_session, user_id=target)
        assert principals == frozenset()
        assert all(not row.is_active for row in await list_identities(db_session, user_id=target))


class TestAuditTrail:
    async def test_linking_writes_a_row_naming_the_actor(self, db_session: AsyncSession) -> None:
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        target = await make_user(db_session, org, "grace", Role.MEMBER)

        linked = await link_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=target,
            source_system=SourceSystem.SLACK,
            subject="U01ABCDEF",
        )

        rows = await audit_rows(db_session, "identity.linked")
        assert len(rows) == 1
        assert uuid.UUID(str(rows[0].actor_id)) == admin
        assert rows[0].resource_type == "source_identity"
        assert uuid.UUID(str(rows[0].resource_id)) == linked.id

    async def test_a_refused_link_writes_nothing(self, db_session: AsyncSession) -> None:
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)

        with pytest.raises(PermissionDenied):
            await link_identity(
                db_session,
                actor_id=admin,
                actor_org_id=org,
                actor_role=Role.IT_ADMIN,
                target_user_id=admin,
                source_system=SourceSystem.SLACK,
                subject="U01VICTIM",
            )

        assert await audit_rows(db_session, "identity.linked") == []

    async def test_revoking_writes_a_row(self, db_session: AsyncSession) -> None:
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        target = await make_user(db_session, org, "grace", Role.MEMBER)
        linked = await link_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=target,
            source_system=SourceSystem.SLACK,
            subject="U01ABCDEF",
        )

        await revoke_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=target,
            identity_id=linked.id,
        )

        rows = await audit_rows(db_session, "identity.revoked")
        assert len(rows) == 1
        assert uuid.UUID(str(rows[0].resource_id)) == linked.id

    async def test_revoke_all_audits_each_identity(self, db_session: AsyncSession) -> None:
        """One row per identity, not one per bulk call: every ACL change is a change."""
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        target = await make_user(db_session, org, "grace", Role.MEMBER)
        for subject in ("U01ABCDEF", "U02ABCDEF"):
            await link_identity(
                db_session,
                actor_id=admin,
                actor_org_id=org,
                actor_role=Role.IT_ADMIN,
                target_user_id=target,
                source_system=SourceSystem.SLACK,
                subject=subject,
            )

        await revoke_all_for_user(
            db_session, actor_id=admin, actor_org_id=org, target_user_id=target
        )

        assert len(await audit_rows(db_session, "identity.revoked")) == 2

    async def test_the_audit_row_carries_no_address_or_subject(
        self, db_session: AsyncSession
    ) -> None:
        """§4.9. Opaque ids only — the resource id is enough to find the row it describes."""
        org = await make_org(db_session, "alpha")
        admin = await make_user(db_session, org, "ida", Role.IT_ADMIN)
        target = await make_user(db_session, org, "grace", Role.MEMBER)
        await link_identity(
            db_session,
            actor_id=admin,
            actor_org_id=org,
            actor_role=Role.IT_ADMIN,
            target_user_id=target,
            source_system=SourceSystem.SLACK,
            subject="U01SECRET",
        )

        dumped = str(
            (
                await db_session.execute(
                    text(
                        "SELECT to_jsonb(audit_log) FROM audit_log "
                        "WHERE resource_type = 'source_identity'"
                    )
                )
            ).all()
        )

        assert "U01SECRET" not in dumped
        assert "grace@example.com" not in dumped
        assert "ida@example.com" not in dumped


class TestRouteDeclarations:
    """The outer gate, asserted against the catalogue rather than described in a docstring."""

    def test_each_route_declares_the_permission_it_should(self) -> None:
        expected = {
            "read_my_identities": Permission.INTEGRATION_SELF_MANAGE,
            "read_employee_identities": Permission.INTEGRATION_READ,
            "create_employee_identity": Permission.INTEGRATION_CONNECT,
            "delete_employee_identity": Permission.INTEGRATION_REVOKE,
        }

        found = {}
        for route in identities_router.router.routes:
            endpoint = route.endpoint  # type: ignore[attr-defined]
            declaration = declaration_of(endpoint)
            assert declaration is not None
            found[endpoint.__name__] = declaration.permission

        assert found == expected

    def test_the_self_route_is_open_to_every_role(self) -> None:
        """Otherwise the people with the least power are least able to check what is linked."""
        assert all(Permission.INTEGRATION_SELF_MANAGE in ROLE_PERMISSIONS[role] for role in Role)

    def test_connecting_is_not_open_to_every_administrator(self) -> None:
        """HR administers people, not integrations. Linking is a data-access grant."""
        assert Permission.INTEGRATION_CONNECT not in ROLE_PERMISSIONS[Role.HR_ADMIN]
        assert Permission.INTEGRATION_CONNECT in ROLE_PERMISSIONS[Role.IT_ADMIN]

    def test_the_link_payload_refuses_unknown_fields(self) -> None:
        """Without `extra="forbid"` a client could post `user_id` or `is_active`, and any
        future widening of this model would silently begin honouring them."""
        with pytest.raises(ValidationError):
            identities_router.LinkPayload(
                source_system=SourceSystem.SLACK,
                subject="U01ABCDEF",
                is_active=True,  # type: ignore[call-arg]
            )
