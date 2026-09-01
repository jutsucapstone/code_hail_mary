"""The operational admin surface, over the wire and against real RLS.

Five capabilities that existed as permissions in the catalogue with no route behind
them: reading the audit trail, reading the job queue, reading sources, listing
invitations, changing a member's role — plus renaming the organisation and the
dashboard's overview counts.

Everything here runs against a real Postgres because what matters is enforced there:
row-level security scoping every list to the caller's organisation, and the audit
trail's immutability. A mocked session would prove none of it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
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

OWNER_EMAIL = "ada@example.com"


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


def csrf(client: AsyncClient) -> dict[str, str]:
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
    email: str,
    full_name: str = "Charles Babbage",
    role: str = "member",
) -> None:
    """Invite and accept. NOTE: leaves the client signed in as the invitee."""
    invited = await client.post(
        "/v1/employees/invitations",
        json={"email": email, "role": role},
        headers=csrf(client),
    )
    assert invited.status_code == 202, invited.text
    token = mailbox.last.secrets["token"]
    accepted = await client.post(
        "/v1/invitations/accept", json={"token": token, "full_name": full_name}
    )
    assert accepted.status_code == 200, accepted.text


async def sign_in(client: AsyncClient, mailbox: RecordingEmailSender, *, email: str) -> None:
    """Switch the client's session to another member of the organisation."""
    requested = await client.post("/v1/auth/request", json={"email": email})
    assert requested.status_code == 202, requested.text
    delivered = mailbox.last.secrets
    verified = await client.post(
        "/v1/auth/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text


async def user_id_of(client: AsyncClient, email: str) -> str:
    """Resolve a member's id through the employees list, as the console would."""
    page = (await client.get("/v1/employees", params={"q": email})).json()
    assert page["items"], f"no employee matching {email}"
    return str(page["items"][0]["id"])


# --------------------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------------------


class TestAuditTrail:
    async def test_a_member_may_not_read_it(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")

        response = await client.get("/v1/audit")
        assert response.status_code == 403

    async def test_real_actions_appear_newest_first(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)

        body = (await client.get("/v1/audit")).json()
        assert body["items"], "registration and acceptance write audit rows"
        stamps = [item["ts"] for item in body["items"]]
        assert stamps == sorted(stamps, reverse=True)

    async def test_no_email_address_appears_anywhere(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """§4.9 — the trail names opaque ids and JUTSU IDs, never mailboxes."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)

        response = await client.get("/v1/audit")
        assert "@example.com" not in response.text

    async def test_filter_by_action_returns_only_that_action(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)

        everything = (await client.get("/v1/audit")).json()["items"]
        assert len({item["action"] for item in everything}) > 1, "need a mixed trail"
        action = everything[0]["action"]

        filtered = (await client.get("/v1/audit", params={"action": action})).json()["items"]
        assert filtered
        assert {item["action"] for item in filtered} == {action}

    async def test_pagination_walks_without_skipping_or_repeating(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)

        first = (await client.get("/v1/audit", params={"limit": 1})).json()
        assert first["next_cursor"]
        second = (
            await client.get("/v1/audit", params={"limit": 1, "cursor": first["next_cursor"]})
        ).json()
        assert second["items"]
        assert second["items"][0]["id"] != first["items"][0]["id"]

    async def test_a_garbage_cursor_is_a_404_not_a_500(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        response = await client.get("/v1/audit", params={"cursor": "not|acursor"})
        assert response.status_code == 404

    async def test_a_cursor_id_beyond_int8_is_a_404_not_a_500(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """`int()` reads numbers Postgres cannot hold; past int8 the driver raises at
        bind time. A crafted cursor must get the same missing page as garbage."""
        await register_owner(client, mailbox)
        crafted = f"2026-01-01T00:00:00+00:00|{2**64}"
        response = await client.get("/v1/audit", params={"cursor": crafted})
        assert response.status_code == 404


# --------------------------------------------------------------------------------------
# Jobs and sources
# --------------------------------------------------------------------------------------


class TestJobsEndpoint:
    async def test_member_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")

        assert (await client.get("/v1/jobs")).status_code == 403
        assert (await client.get("/v1/jobs/stats")).status_code == 403

    async def test_empty_queue_is_an_empty_page_not_an_error(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)

        body = (await client.get("/v1/jobs")).json()
        assert body == {"items": [], "next_cursor": None}

        stats = (await client.get("/v1/jobs/stats")).json()
        assert stats == {"by_state": {}, "dead_letter": 0, "failed_24h": 0}

    async def test_a_job_row_is_visible_with_its_classification_and_no_error_text(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """The list carries `failure_kind`, never the exception string (§4.9)."""
        await register_owner(client, mailbox)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
        )
        await db_session.execute(
            text(
                "INSERT INTO jobs (id, org_id, kind, state, idempotency_key, attempts, "
                "failure_kind, error) VALUES (gen_random_uuid(), :org, 'ingest_document', "
                "'failed', 'test-key-1', 3, 'transient', "
                "'Exception at C:/secret/path/leaky.txt')"
            ),
            {"org": org_id},
        )

        response = await client.get("/v1/jobs")
        body = response.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["failure_kind"] == "transient"
        assert "leaky" not in response.text

        stats = (await client.get("/v1/jobs/stats")).json()
        assert stats["by_state"] == {"failed": 1}
        assert stats["failed_24h"] == 1


class TestSourcesEndpoint:
    async def test_member_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")

        assert (await client.get("/v1/sources")).status_code == 403

    async def test_a_source_is_listed_without_its_configuration(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Connector state is the surface; corpus paths in `config_json` are not."""
        await register_owner(client, mailbox)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
        )
        await db_session.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json, status) VALUES "
                "(gen_random_uuid(), :org, 'local', "
                "cast('{\"root\": \"C:/corpora/enron-secret-path\"}' AS jsonb), 'idle')"
            ),
            {"org": org_id},
        )

        response = await client.get("/v1/sources")
        body = response.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["system"] == "local"
        assert body["items"][0]["document_count"] == 0
        assert "enron-secret-path" not in response.text

    async def test_job_counters_match_the_workers_key_format(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """The per-source counters LIKE-match keys the worker builds; pin both halves.

        The key here comes from `document_job_key` itself, not a copied string, so a
        change to either the worker's format or the query's prefix fails this test
        instead of silently zeroing every counter on the sources page.
        """
        from jutsu_worker.ingest import document_job_key

        await register_owner(client, mailbox)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
        )
        source_id = uuid.uuid4()
        other_source_id = uuid.uuid4()
        for sid in (source_id, other_source_id):
            await db_session.execute(
                text(
                    "INSERT INTO sources (id, org_id, system, config_json, status) "
                    "VALUES (:id, :org, 'local', '{}'::jsonb, 'idle')"
                ),
                {"id": sid, "org": org_id},
            )
        await db_session.execute(
            text(
                "INSERT INTO jobs (id, org_id, kind, state, idempotency_key) VALUES "
                "(gen_random_uuid(), :org, 'ingest.document', 'pending', :key)"
            ),
            {"org": org_id, "key": document_job_key(uuid.UUID(org_id), source_id, "msg-001")},
        )
        await db_session.execute(
            text(
                "INSERT INTO jobs (id, org_id, kind, state, idempotency_key) VALUES "
                "(gen_random_uuid(), :org, 'ingest.document', 'completed', :key)"
            ),
            {"org": org_id, "key": document_job_key(uuid.UUID(org_id), source_id, "msg-002")},
        )

        body = (await client.get("/v1/sources")).json()
        by_id = {item["id"]: item for item in body["items"]}
        counted = by_id[str(source_id)]
        assert counted["jobs_pending"] == 1
        assert counted["jobs_completed"] == 1
        assert counted["jobs_failed"] == 0

        other = by_id[str(other_source_id)]
        assert other["jobs_pending"] == 0, "keys are source-scoped; nothing bleeds across"
        assert other["jobs_completed"] == 0


# --------------------------------------------------------------------------------------
# Invitations
# --------------------------------------------------------------------------------------


class TestInvitationList:
    async def test_a_pending_invitation_is_listed_as_pending(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        sent = await client.post(
            "/v1/employees/invitations",
            json={"email": "pending@example.com", "role": "member"},
            headers=csrf(client),
        )
        assert sent.status_code == 202

        body = (await client.get("/v1/invitations")).json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["email"] == "pending@example.com"
        assert item["status"] == "pending"
        assert item["role"] == "member"

    async def test_an_accepted_invitation_flips_to_accepted(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="joined@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)

        body = (await client.get("/v1/invitations")).json()
        assert body["items"][0]["status"] == "accepted"

    async def test_a_member_may_not_see_who_was_invited(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")

        assert (await client.get("/v1/invitations")).status_code == 403


# --------------------------------------------------------------------------------------
# Role assignment
# --------------------------------------------------------------------------------------


class TestRoleAssignment:
    async def test_owner_promotes_a_member_and_the_change_is_audited(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="rising@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        target = await user_id_of(client, "rising@example.com")

        response = await client.patch(
            f"/v1/employees/{target}/role", json={"role": "hr_admin"}, headers=csrf(client)
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "user_id": target,
            "role": "hr_admin",
            "previous_role": "member",
        }

        # The change is real: the employees list now reports the new role…
        page = (await client.get("/v1/employees", params={"q": "rising@example.com"})).json()
        assert page["items"][0]["role"] == "hr_admin"

        # …and the trail recorded who did it and both roles.
        trail = (await client.get("/v1/audit", params={"action": "member.role_changed"})).json()
        assert len(trail["items"]) == 1
        assert trail["items"][0]["resource_id"] == target
        assert trail["items"][0]["outcome"] == "success"

    async def test_changing_your_own_role_is_refused_even_for_the_owner(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        me = (await client.get("/v1/me")).json()

        response = await client.patch(
            f"/v1/employees/{me['user_id']}/role",
            json={"role": "viewer"},
            headers=csrf(client),
        )
        assert response.status_code == 403

    async def test_a_peer_cannot_be_touched(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """HR Admin and IT Admin are equal ranks with disjoint powers."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="it@example.com", role="it_admin")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await invite_and_accept(client, mailbox, email="hr@example.com", role="hr_admin")
        await sign_in(client, mailbox, email="hr@example.com")
        target = await user_id_of(client, "it@example.com")

        response = await client.patch(
            f"/v1/employees/{target}/role", json={"role": "member"}, headers=csrf(client)
        )
        assert response.status_code == 403

    async def test_nobody_can_grant_their_own_rank(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="hr@example.com", role="hr_admin")
        await sign_in(client, mailbox, email="hr@example.com")
        await invite_and_accept(client, mailbox, email="junior@example.com")
        await sign_in(client, mailbox, email="hr@example.com")
        target = await user_id_of(client, "junior@example.com")

        response = await client.patch(
            f"/v1/employees/{target}/role", json={"role": "hr_admin"}, headers=csrf(client)
        )
        assert response.status_code == 403

    async def test_owner_is_structurally_unassignable(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Nothing outranks owner, so nothing can grant it — not even the owner."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        target = await user_id_of(client, "member@example.com")

        response = await client.patch(
            f"/v1/employees/{target}/role", json={"role": "owner"}, headers=csrf(client)
        )
        assert response.status_code == 403

    async def test_a_made_up_user_is_404(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        response = await client.patch(
            "/v1/employees/00000000-0000-4000-8000-000000000000/role",
            json={"role": "member"},
            headers=csrf(client),
        )
        assert response.status_code == 404

    async def test_a_member_may_not_assign_roles_at_all(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        me_owner = (await client.get("/v1/me")).json()
        await invite_and_accept(client, mailbox, email="member@example.com")

        response = await client.patch(
            f"/v1/employees/{me_owner['user_id']}/role",
            json={"role": "member"},
            headers=csrf(client),
        )
        assert response.status_code == 403


# --------------------------------------------------------------------------------------
# Organisation settings and overview
# --------------------------------------------------------------------------------------


class TestOrganisationSettings:
    async def test_rename_takes_effect_and_is_audited(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)

        response = await client.patch(
            "/v1/orgs/current", json={"name": "Example Analytical Ltd"}, headers=csrf(client)
        )
        assert response.status_code == 200
        assert response.json() == {"name": "Example Analytical Ltd"}

        profile = (await client.get("/v1/orgs/current")).json()
        assert profile["name"] == "Example Analytical Ltd"

        trail = (await client.get("/v1/audit", params={"action": "org.renamed"})).json()
        assert len(trail["items"]) == 1

    async def test_a_member_may_not_rename(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")

        response = await client.patch(
            "/v1/orgs/current", json={"name": "Hijacked"}, headers=csrf(client)
        )
        assert response.status_code == 403

    async def test_a_blank_name_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        response = await client.patch(
            "/v1/orgs/current", json={"name": "   "}, headers=csrf(client)
        )
        assert response.status_code == 422

    async def test_unknown_fields_are_rejected(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """`domain` is deliberately not editable; sending it must fail loudly."""
        await register_owner(client, mailbox)
        response = await client.patch(
            "/v1/orgs/current",
            json={"name": "Fine", "domain": "attacker.example"},
            headers=csrf(client),
        )
        assert response.status_code == 422


class TestOverview:
    async def test_counts_are_real(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        sent = await client.post(
            "/v1/employees/invitations",
            json={"email": "pending@example.com", "role": "member"},
            headers=csrf(client),
        )
        assert sent.status_code == 202

        body = (await client.get("/v1/orgs/current/overview")).json()
        assert body["documents"] == 0
        assert body["sources"] == 0
        assert body["invitations_pending"] == 1
        assert body["audit_events_24h"] >= 1, "registration itself is audited"

    async def test_member_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")

        assert (await client.get("/v1/orgs/current/overview")).status_code == 403


class TestRoleCatalogue:
    async def test_the_catalogue_comes_from_the_database(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)

        body = (await client.get("/v1/roles")).json()
        by_key = {role["key"]: role for role in body["roles"]}
        assert set(by_key) == {
            "owner",
            "super_admin",
            "hr_admin",
            "it_admin",
            "analyst",
            "viewer",
            "member",
        }
        assert by_key["owner"]["rank"] > by_key["member"]["rank"]
        assert "org:delete" in by_key["owner"]["permissions"]
        assert "org:delete" not in by_key["super_admin"]["permissions"]
        assert by_key["hr_admin"]["label"] == "HR Admin"

    async def test_a_member_may_not_browse_the_admin_surface_map(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")

        assert (await client.get("/v1/roles")).status_code == 403


class TestDepartments:
    async def test_departments_aggregate_what_people_declared(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        saved = await client.patch(
            "/v1/me/profile", json={"department": "Platform"}, headers=csrf(client)
        )
        assert saved.status_code == 200

        body = (await client.get("/v1/departments")).json()
        assert body["items"] == [{"name": "Platform", "members": 1}]
        assert body["unassigned"] == 0

    async def test_people_without_a_department_are_counted_not_hidden(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)

        body = (await client.get("/v1/departments")).json()
        assert body["items"] == []
        assert body["unassigned"] == 1


class TestMyKnowledge:
    async def test_no_linked_identity_means_zero_and_the_shape_says_so(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]
        me = (await client.get("/v1/me")).json()

        # A document granted to somebody else entirely.
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
        )
        source_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json) "
                "VALUES (:id, :org, 'local', '{}'::jsonb)"
            ),
            {"id": source_id, "org": org_id},
        )
        doc = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO documents (id, org_id, source_id, external_id, title, "
                "content_hash, acl_hash, body_original, body_masked, created_at) "
                "VALUES (:id, :org, :src, 'x', 'Private doc', 'h', 'a', 'b', 'b', now())"
            ),
            {"id": doc, "org": org_id, "src": source_id},
        )
        await db_session.execute(
            text(
                "INSERT INTO document_acl (document_id, principal_type, principal_id, "
                "org_id, permission) VALUES (:doc, 'user', 'local:someone-else', :org, 'read')"
            ),
            {"doc": doc, "org": org_id},
        )
        # Deactivate the caller's auto-linked identity: zero principals, fail closed.
        await db_session.execute(
            text("UPDATE source_identities SET is_active = false WHERE user_id = :uid"),
            {"uid": me["user_id"]},
        )
        await db_session.commit()

        body = (await client.get("/v1/me/knowledge")).json()
        assert body["total_documents"] == 0
        assert body["by_source"] == []
        assert body["recent"] == []
        assert body["linked_identities"] == 0

    async def test_a_granted_document_is_counted_and_listed(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
        )
        source_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json) "
                "VALUES (:id, :org, 'local', '{}'::jsonb)"
            ),
            {"id": source_id, "org": org_id},
        )
        doc = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO documents (id, org_id, source_id, external_id, title, "
                "content_hash, acl_hash, body_original, body_masked, created_at) "
                "VALUES (:id, :org, :src, 'x', 'My design doc', 'h', 'a', 'b', 'b', now())"
            ),
            {"id": doc, "org": org_id, "src": source_id},
        )
        # Registration auto-linked local:ada@example.com — grant to that principal.
        await db_session.execute(
            text(
                "INSERT INTO document_acl (document_id, principal_type, principal_id, "
                "org_id, permission) "
                "VALUES (:doc, 'user', 'local:ada@example.com', :org, 'read')"
            ),
            {"doc": doc, "org": org_id},
        )
        await db_session.commit()

        body = (await client.get("/v1/me/knowledge")).json()
        assert body["total_documents"] == 1
        assert body["by_source"] == [{"source_system": "local", "documents": 1}]
        assert body["recent"][0]["title"] == "My design doc"
        assert body["linked_identities"] == 1


class TestRoleTitle:
    async def test_a_written_title_lands_as_the_invitees_designation(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """§1's free-text option: vocabulary on the profile, authority from the catalogue."""
        await register_owner(client, mailbox)
        sent = await client.post(
            "/v1/employees/invitations",
            json={
                "email": "ops@example.com",
                "role": "it_admin",
                "role_title": "Head of Platform Operations",
            },
            headers=csrf(client),
        )
        assert sent.status_code == 202, sent.text
        token = mailbox.last.secrets["token"]
        accepted = await client.post(
            "/v1/invitations/accept", json={"token": token, "full_name": "Ops Person"}
        )
        assert accepted.status_code == 200, accepted.text

        # Signed in as the invitee now: the title is on their own profile…
        profile = (await client.get("/v1/me/profile")).json()
        assert profile["designation"] == "Head of Platform Operations"
        # …while their authority is exactly the catalogued role, nothing more.
        me = (await client.get("/v1/me")).json()
        assert me["role"] == "it_admin"

    async def test_the_title_confers_no_permission(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """A member titled "Chief Everything Officer" is still a member."""
        await register_owner(client, mailbox)
        sent = await client.post(
            "/v1/employees/invitations",
            json={
                "email": "grand@example.com",
                "role": "member",
                "role_title": "Chief Everything Officer",
            },
            headers=csrf(client),
        )
        assert sent.status_code == 202
        token = mailbox.last.secrets["token"]
        await client.post(
            "/v1/invitations/accept", json={"token": token, "full_name": "Grand Title"}
        )

        assert (await client.get("/v1/employees")).status_code == 403
        assert (await client.get("/v1/audit")).status_code == 403
