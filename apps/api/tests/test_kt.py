"""Knowledge transfer, over the wire, against real Postgres, RLS and the audit trail.

§47's KT matrix, in test names: valid access, invalid code, expired, revoked, wrong
organisation, unauthorized recipient — plus the lifecycle around them and the property
underneath all of them: a KT code is never an access key.
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
    full_name: str = "Grace Hopper",
    role: str = "member",
) -> None:
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
    await client.post("/v1/auth/request", json={"email": email})
    delivered = mailbox.last.secrets
    verified = await client.post(
        "/v1/auth/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text


async def user_id_of(client: AsyncClient, email: str) -> str:
    page = (await client.get("/v1/employees", params={"q": email})).json()
    assert page["items"], f"no employee matching {email}"
    return str(page["items"][0]["id"])


async def create_kt(
    client: AsyncClient,
    *,
    subject_user_id: str,
    scope: list[str] | None = None,
    validity_days: int = 30,
    recipient_email: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "subject_user_id": subject_user_id,
        "scope": scope or ["documents", "profile"],
        "validity_days": validity_days,
    }
    if recipient_email:
        payload["recipient_email"] = recipient_email
    response = await client.post("/v1/kt", json=payload, headers=csrf(client))
    assert response.status_code == 201, response.text
    return dict(response.json())


class TestLifecycle:
    async def test_create_returns_a_wellformed_code_and_audits(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")

        package = await create_kt(client, subject_user_id=subject)
        assert str(package["kt_code"]).startswith("KT-JUTSU-")
        assert len(str(package["kt_code"])) == len("KT-JUTSU-XXXXXXXX")
        assert package["status"] == "active"
        assert package["subject_name"] == "Grace Hopper"

        trail = (await client.get("/v1/audit", params={"action": "kt.created"})).json()
        assert len(trail["items"]) == 1

    async def test_scope_the_backend_cannot_serve_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """§13: never offer, and never accept, a category with nothing behind it."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")

        # "decisions" became servable when extraction landed; a category with nothing
        # behind it must still be refused by name.
        response = await client.post(
            "/v1/kt",
            json={"subject_user_id": subject, "scope": ["astrology"], "validity_days": 30},
            headers=csrf(client),
        )
        assert response.status_code == 422
        assert "astrology" in response.json()["error"]["message"]

    async def test_a_member_cannot_manage_packages(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")

        assert (await client.get("/v1/kt")).status_code == 403

    async def test_hr_admin_can_manage_and_it_admin_cannot(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Transitions are HR's domain; IT governs connections instead."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="hr@example.com", role="hr_admin")
        assert (await client.get("/v1/kt")).status_code == 200

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await invite_and_accept(client, mailbox, email="it@example.com", role="it_admin")
        assert (await client.get("/v1/kt")).status_code == 403

    async def test_the_list_shows_derived_status(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject)

        revoked = await client.post(f"/v1/kt/{package['id']}/revoke", headers=csrf(client))
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"

        listing = (await client.get("/v1/kt")).json()
        assert listing["items"][0]["status"] == "revoked"


class TestOpening:
    async def test_valid_access_claims_and_returns_the_recipient_view(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject)

        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New Hire")
        opened = await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert body["kt_code"] == package["kt_code"]
        assert body["subject"]["display_name"] == "Grace Hopper"
        # The recipient view must not carry the subject's email or the admin fields.
        assert "subject_email" not in body
        assert "leaver@example.com" not in opened.text

    async def test_an_invalid_code_is_a_404(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        response = await client.post(
            "/v1/kt/claim", json={"kt_code": "KT-JUTSU-00000000"}, headers=csrf(client)
        )
        assert response.status_code == 404

    async def test_a_revoked_package_says_exactly_that(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """§39's sentence comes from the server, so no cache can soften it."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject)
        await client.post(f"/v1/kt/{package['id']}/revoke", headers=csrf(client))

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        response = await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        assert response.status_code == 403
        assert (
            response.json()["error"]["message"]
            == "This Knowledge Transfer package has been revoked."
        )

    async def test_an_expired_package_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject, validity_days=1)

        org_id = (await client.get("/v1/orgs/current")).json()["id"]
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
        )
        await db_session.execute(
            text("UPDATE kt_packages SET expires_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": package["id"]},
        )

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        response = await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        assert response.status_code == 403
        assert "expired" in response.json()["error"]["message"].lower()

    async def test_a_bound_package_is_invisible_to_the_wrong_recipient(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Unauthorized recipient — a 404, not a 403: a bound package must not confirm
        its own existence to whoever happens to hold the code."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(
            client, subject_user_id=subject, recipient_email="intended@example.com"
        )

        await invite_and_accept(client, mailbox, email="wrong@example.com")
        response = await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        assert response.status_code == 404

    async def test_the_intended_recipient_opens_a_bound_package(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(
            client, subject_user_id=subject, recipient_email="intended@example.com"
        )

        await invite_and_accept(client, mailbox, email="intended@example.com")
        response = await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        assert response.status_code == 200

    async def test_first_claim_locks_everyone_else_out(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject)

        await invite_and_accept(client, mailbox, email="first@example.com")
        assert (
            await client.post(
                "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
            )
        ).status_code == 200

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await invite_and_accept(client, mailbox, email="second@example.com")
        stolen = await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        assert stolen.status_code == 404

        # …while the claimer can re-open freely.
        await sign_in(client, mailbox, email="first@example.com")
        again = await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        assert again.status_code == 200

    async def test_a_hand_typed_code_is_forgiven_its_crockford_confusables(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """O for 0 and l for 1 are transcription, not identity."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject)

        mangled = str(package["kt_code"]).lower().replace("0", "o").replace("1", "i")
        await invite_and_accept(client, mailbox, email="newhire@example.com")
        response = await client.post(
            "/v1/kt/claim", json={"kt_code": mangled}, headers=csrf(client)
        )
        assert response.status_code == 200

    async def test_denied_opens_land_in_the_audit_trail(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """A stream of refused codes is a probe; the trail is where it becomes visible."""
        await register_owner(client, mailbox)
        await client.post(
            "/v1/kt/claim", json={"kt_code": "KT-JUTSU-00000000"}, headers=csrf(client)
        )

        trail = (
            await client.get("/v1/audit", params={"action": "kt.open", "outcome": "denied"})
        ).json()
        assert len(trail["items"]) == 1


class TestKtDocuments:
    async def test_documents_come_from_the_recipients_own_acl(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """The package narrows presentation; the recipient's grants decide visibility.

        Two documents exist. The recipient's principal is on the ACL of exactly one.
        The KT documents list returns exactly that one — the package does not leak the
        other, however in-scope and in-period it is.
        """
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New Hire")
        # Registration linked local:newhire@example.com automatically (ADR 0010's gap
        # closure) — that is the principal the visible document is granted to.
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
        visible, hidden = uuid.uuid4(), uuid.uuid4()
        for doc_id, title in ((visible, "Visible design doc"), (hidden, "Hidden budget")):
            await db_session.execute(
                text(
                    "INSERT INTO documents (id, org_id, source_id, external_id, title, "
                    "content_hash, acl_hash, body_original, body_masked, created_at) "
                    "VALUES (:id, :org, :src, :ext, :title, :ext, 'a', 'b', 'b', now())"
                ),
                {"id": doc_id, "org": org_id, "src": source_id, "ext": title, "title": title},
            )
        await db_session.execute(
            text(
                "INSERT INTO document_acl (document_id, principal_type, principal_id, "
                "org_id, permission) VALUES (:doc, 'user', :pid, :org, 'read')"
            ),
            {"doc": visible, "pid": "local:newhire@example.com", "org": org_id},
        )
        await db_session.execute(
            text(
                "INSERT INTO document_acl (document_id, principal_type, principal_id, "
                "org_id, permission) VALUES (:doc, 'user', :pid, :org, 'read')"
            ),
            {"doc": hidden, "pid": "local:somebody-else@example.com", "org": org_id},
        )
        await db_session.commit()

        await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        response = await client.get(f"/v1/kt/{package['kt_code']}/documents")
        assert response.status_code == 200, response.text
        body = response.json()
        titles = [item["title"] for item in body["items"]]
        assert titles == ["Visible design doc"]
        assert "Hidden budget" not in response.text

    async def test_a_recipient_with_no_grants_gets_an_empty_page_not_an_error(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        # Revoke the recipient's automatically linked identity, leaving them zero
        # principals — the fail-closed default.
        me = (await client.get("/v1/me")).json()
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
        )
        await db_session.execute(
            text("UPDATE source_identities SET is_active = false WHERE user_id = :uid"),
            {"uid": me["user_id"]},
        )
        await db_session.commit()

        await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        response = await client.get(f"/v1/kt/{package['kt_code']}/documents")
        assert response.status_code == 200
        assert response.json()["items"] == []

    async def test_revocation_closes_the_documents_window_immediately(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """§39 — no cache may outlive the revocation, because every read re-opens."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject)

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        assert (await client.get(f"/v1/kt/{package['kt_code']}/documents")).status_code == 200

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await client.post(f"/v1/kt/{package['id']}/revoke", headers=csrf(client))

        await sign_in(client, mailbox, email="newhire@example.com")
        closed = await client.get(f"/v1/kt/{package['kt_code']}/documents")
        assert closed.status_code == 403
        assert "revoked" in closed.json()["error"]["message"].lower()

    async def test_scope_without_documents_refuses_the_documents_window(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject, scope=["profile"])

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        response = await client.get(f"/v1/kt/{package['kt_code']}/documents")
        assert response.status_code == 403


class TestKtInsights:
    async def seed_claims(self, client: AsyncClient, db_session: AsyncSession, org_id: str) -> None:
        """Two decision claims from finished runs — one on a document the recipient's
        principal can read, one on a document granted to somebody else."""
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
        for title, principal, quote in (
            ("Visible decision doc", "local:newhire@example.com", "we chose PostgreSQL"),
            ("Hidden decision doc", "local:somebody-else", "we chose MongoDB"),
        ):
            doc_id = uuid.uuid4()
            await db_session.execute(
                text(
                    "INSERT INTO documents (id, org_id, source_id, external_id, title, "
                    "content_hash, acl_hash, body_original, body_masked, created_at) "
                    "VALUES (:id, :org, :src, :ext, :title, :ext, 'a', :q, :q, now())"
                ),
                {
                    "id": doc_id,
                    "org": org_id,
                    "src": source_id,
                    "ext": title,
                    "title": title,
                    "q": quote,
                },
            )
            chunk_id = uuid.uuid4()
            await db_session.execute(
                text(
                    "INSERT INTO chunks (id, document_id, org_id, ordinal, text, "
                    "char_start, char_end, token_count) "
                    "VALUES (:id, :doc, :org, 0, :text, 0, :end, 5)"
                ),
                {
                    "id": chunk_id,
                    "doc": doc_id,
                    "org": org_id,
                    "text": quote,
                    "end": len(quote),
                },
            )
            await db_session.execute(
                text(
                    "INSERT INTO document_acl (document_id, principal_type, principal_id, "
                    "org_id, permission) VALUES (:doc, 'user', :pid, :org, 'read')"
                ),
                {"doc": doc_id, "pid": principal, "org": org_id},
            )
            # The run's stats name the document; the read model's "current" is the
            # latest finished run per document.
            per_doc_run = uuid.uuid4()
            await db_session.execute(
                text(
                    "INSERT INTO extraction_runs (id, org_id, extractor_version, "
                    "prompt_hash, model, finished_at, stats_json) "
                    "VALUES (:id, :org, 'v1', 'h', 'test', now(), cast(:stats AS jsonb))"
                ),
                {
                    "id": per_doc_run,
                    "org": org_id,
                    "stats": '{"document_id": "' + str(doc_id) + '"}',
                },
            )
            await db_session.execute(
                text(
                    "INSERT INTO extraction_claims (id, run_id, chunk_id, org_id, "
                    "claim_type, payload_json, confidence) "
                    "VALUES (gen_random_uuid(), :run, :chunk, :org, 'decision', "
                    "cast(:payload AS jsonb), 0.9)"
                ),
                {
                    "run": per_doc_run,
                    "chunk": chunk_id,
                    "org": org_id,
                    "payload": (
                        '{"summary": "' + title + '", "quote": "' + quote + '", '
                        '"document_id": "' + str(doc_id) + '"}'
                    ),
                },
            )
        await db_session.commit()

    async def test_insights_are_filtered_by_the_recipients_own_acl(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Non-negotiable 6: a claim whose evidence the caller cannot read is invisible."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject, scope=["decisions", "documents"])
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        await self.seed_claims(client, db_session, org_id)

        await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        response = await client.get(
            f"/v1/kt/{package['kt_code']}/insights", params={"type": "decision"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["document_title"] for item in body["items"]] == ["Visible decision doc"]
        assert "MongoDB" not in response.text

        summary = (await client.get(f"/v1/kt/{package['kt_code']}/insights-summary")).json()
        assert summary["by_type"] == {"decision": 1}

    async def test_a_type_outside_the_packages_scope_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject, scope=["documents"])

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        response = await client.get(
            f"/v1/kt/{package['kt_code']}/insights", params={"type": "decision"}
        )
        assert response.status_code == 403

    async def test_the_widened_scope_catalogue_is_served(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """§13's wizard offers exactly what the backend can now fill."""
        await register_owner(client, mailbox)
        body = (await client.get("/v1/kt/scopes")).json()
        assert set(body["supported"]) == {
            "documents",
            "profile",
            "decisions",
            "people",
            "projects",
            "meetings",
            "responsibilities",
        }

    async def test_revocation_closes_insights_immediately(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject, scope=["decisions"])

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await client.post(f"/v1/kt/{package['id']}/revoke", headers=csrf(client))
        await sign_in(client, mailbox, email="newhire@example.com")

        closed = await client.get(
            f"/v1/kt/{package['kt_code']}/insights", params={"type": "decision"}
        )
        assert closed.status_code == 403
        assert "revoked" in closed.json()["error"]["message"].lower()


class ScriptedAnswers:
    """An answer transport that returns what the test scripted, recording prompts."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.prompts: list[str] = []

    async def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0)


@pytest.fixture
async def handover(
    db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
) -> AsyncIterator[tuple[AsyncClient, ScriptedAnswers]]:
    """The standard client plus a scripted answer transport behind the ask seam."""
    from jutsu_api.routers.search import get_answer_transport

    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    scripted = ScriptedAnswers()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox
    app.dependency_overrides[get_answer_transport] = lambda: scripted

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as http:
        yield http, scripted


class TestKtHandoverSummary:
    """§29's executive summary: grounded on the recipient's own claim visibility."""

    async def test_grounds_only_on_visible_claims_and_cites_them(
        self,
        handover: tuple[AsyncClient, ScriptedAnswers],
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")
        client, scripted = handover
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject, scope=["decisions", "documents"])
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        await TestKtInsights().seed_claims(client, db_session, org_id)
        await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )

        scripted.replies = ["The team standardised on PostgreSQL [1]."]
        response = await client.get(f"/v1/kt/{package['kt_code']}/handover-summary")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["insufficient_evidence"] is False
        assert "PostgreSQL" in body["summary"]
        assert [c["document_title"] for c in body["citations"]] == ["Visible decision doc"]
        # Non-negotiable 6 holds for the composer too: the hidden claim never even
        # reaches the model's prompt.
        assert "MongoDB" not in scripted.prompts[0]

    async def test_an_unciteable_summary_is_refused_honestly(
        self,
        handover: tuple[AsyncClient, ScriptedAnswers],
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")
        client, scripted = handover
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject, scope=["decisions", "documents"])
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        await TestKtInsights().seed_claims(client, db_session, org_id)
        await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )

        scripted.replies = ["A fluent uncited paragraph.", "Still no citations."]
        response = await client.get(f"/v1/kt/{package['kt_code']}/handover-summary")
        assert response.status_code == 200
        body = response.json()
        assert body["summary"] is None
        assert body["insufficient_evidence"] is True
        assert body["attempts"] == 2

    async def test_no_visible_claims_refuses_without_spending(
        self,
        handover: tuple[AsyncClient, ScriptedAnswers],
        mailbox: RecordingEmailSender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")
        client, scripted = handover
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject, scope=["decisions"])

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        response = await client.get(f"/v1/kt/{package['kt_code']}/handover-summary")
        assert response.status_code == 200
        body = response.json()
        assert body["insufficient_evidence"] is True
        assert body["attempts"] == 0
        assert scripted.prompts == [], "no evidence means no model call, no spend"

    async def test_without_a_model_the_refusal_names_the_configuration(
        self,
        handover: tuple[AsyncClient, ScriptedAnswers],
        mailbox: RecordingEmailSender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client, _scripted = handover
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        package = await create_kt(client, subject_user_id=subject, scope=["decisions"])

        await invite_and_accept(client, mailbox, email="newhire@example.com")
        await client.post(
            "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
        )
        response = await client.get(f"/v1/kt/{package['kt_code']}/handover-summary")
        assert response.status_code == 503
        assert "not configured" in response.json()["error"]["message"]
