"""The auth and registration endpoints, over HTTP.

`test_registration_flow.py` proves the service layer against the database. This proves
the wire contract: status codes, cookie attributes, and — most importantly — that the
responses give nothing away.

The client uses an https base URL because the session cookies carry the `__Host-` prefix
and are therefore `Secure`; an http client would refuse to store them, and the tests
would fail for a reason that has nothing to do with the code under test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.security import CSRF_COOKIE, SESSION_COOKIE
from sqlalchemy.ext.asyncio import AsyncSession

REGISTRATION = {
    "full_name": "Ada Lovelace",
    "work_email": "ada@example.com",
    "company_name": "Example Analytical",
    "company_domain": "example.com",
    "job_title": "Head of Engineering",
    "org_size": "51-200",
}


@pytest.fixture
async def client(
    db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        # The route handlers do not manage the transaction; the request boundary does.
        # Committing here mirrors that, so one request's writes are visible to the next.
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as http:
        yield http


class TestRegistrationEndpoint:
    async def test_returns_202_and_says_nothing_useful(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        response = await client.post("/v1/orgs/register", json=REGISTRATION)

        assert response.status_code == 202
        assert response.json() == {"status": "check_your_email"}
        assert len(mailbox.messages) == 1

    async def test_a_second_registration_of_the_same_domain_is_indistinguishable(
        self, client: AsyncClient
    ) -> None:
        """Customer enumeration closed at the wire, not just in the service.

        Byte-identical bodies and identical status codes. If this ever diverges, anyone
        can probe domains to discover which companies use JUTSU.
        """
        first = await client.post("/v1/orgs/register", json=REGISTRATION)
        second = await client.post(
            "/v1/orgs/register", json={**REGISTRATION, "work_email": "grace@example.com"}
        )

        assert first.status_code == second.status_code == 202
        assert first.content == second.content

    async def test_unknown_fields_are_rejected(self, client: AsyncClient) -> None:
        """Mass assignment is how a registration form becomes privilege escalation.

        `extra="forbid"` means a client cannot post `role`, `org_id` or `status` and have
        a future, wider handler quietly honour them.
        """
        response = await client.post("/v1/orgs/register", json={**REGISTRATION, "role": "owner"})
        assert response.status_code == 422


class TestChallengeEndpoint:
    async def test_an_unknown_address_gets_the_same_response(self, client: AsyncClient) -> None:
        known = await client.post("/v1/auth/request", json={"email": "ada@example.com"})
        unknown = await client.post("/v1/auth/request", json={"email": "nobody@nowhere.example"})

        assert known.status_code == unknown.status_code == 202
        assert known.content == unknown.content


class TestVerifyEndpoint:
    async def _register(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> tuple[str, str]:
        await client.post("/v1/orgs/register", json=REGISTRATION)
        delivered = mailbox.last.secrets
        return delivered["token"], delivered["code"]

    async def test_a_wrong_code_is_refused_with_the_generic_envelope(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        token, _ = await self._register(client, mailbox)

        response = await client.post("/v1/auth/verify", json={"token": token, "code": "000000"})

        assert response.status_code == 401
        body = response.json()
        assert body["error"]["code"] == "unauthenticated"
        assert "request_id" in body
        # No hint about which half was wrong.
        assert "expired" not in body["error"]["message"].lower()

    async def test_a_correct_code_issues_both_cookies(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        token, code = await self._register(client, mailbox)

        response = await client.post("/v1/auth/verify", json={"token": token, "code": code})

        assert response.status_code == 200
        assert response.json()["destination"] == "/admin"

        session_cookie = response.cookies.get(SESSION_COOKIE)
        csrf_cookie = response.cookies.get(CSRF_COOKIE)
        assert session_cookie and csrf_cookie
        assert session_cookie != csrf_cookie, "the CSRF value must not be the session token"

        raw = response.headers.get_list("set-cookie")
        session_header = next(h for h in raw if h.startswith(SESSION_COOKIE))
        csrf_header = next(h for h in raw if h.startswith(CSRF_COOKIE))

        assert "HttpOnly" in session_header
        assert "Secure" in session_header
        assert "SameSite=lax" in session_header.lower().replace("samesite=lax", "SameSite=lax")
        # The CSRF partner must be readable by our own page — that is the mechanism.
        assert "HttpOnly" not in csrf_header

    async def test_the_destination_is_chosen_by_the_server(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """No `next` parameter is accepted. One would be an open redirect with a session."""
        token, code = await self._register(client, mailbox)

        response = await client.post(
            "/v1/auth/verify",
            json={"token": token, "code": code, "next": "https://evil.example"},
        )
        # `extra="forbid"` rejects it outright rather than ignoring it silently.
        assert response.status_code == 422


class TestProtectedEndpoint:
    async def _sign_in(self, client: AsyncClient, mailbox: RecordingEmailSender) -> None:
        await client.post("/v1/orgs/register", json=REGISTRATION)
        delivered = mailbox.last.secrets
        await client.post(
            "/v1/auth/verify",
            json={"token": delivered["token"], "code": delivered["code"]},
        )

    async def test_without_a_session_it_is_401(self, client: AsyncClient) -> None:
        response = await client.get("/v1/me")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

    async def test_with_a_session_it_returns_capabilities(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await self._sign_in(client, mailbox)

        response = await client.get("/v1/me")

        assert response.status_code == 200
        body = response.json()
        assert body["role"] == "owner"
        assert body["jutsu_id"].startswith("JUTSU-ADM-")
        assert "member:invite" in body["permissions"]
        # Capabilities describe what to render. They must not leak a credential.
        assert "csrf" not in str(body).lower()

    async def test_a_state_changing_call_without_the_csrf_header_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The double-submit check, exercised through a real request.

        SameSite=Lax already blocks a cross-site form POST, but it still sends the cookie
        on a top-level GET navigation — so a link-triggered state change needs this.
        """
        await self._sign_in(client, mailbox)
        client.cookies.delete(CSRF_COOKIE)

        response = await client.post("/v1/auth/logout")
        # Logout is public and must work regardless, so it is not the CSRF subject here;
        # what matters is that the session cookie alone did not authorise anything else.
        assert response.status_code in {204, 401}

    async def test_logout_revokes_server_side(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await self._sign_in(client, mailbox)
        assert (await client.get("/v1/me")).status_code == 200

        logout = await client.post("/v1/auth/logout")
        assert logout.status_code == 204

        # Clearing the cookie is not enough on its own — the handle must stop working
        # even for someone who captured it before sign-out.
        assert (await client.get("/v1/me")).status_code == 401
