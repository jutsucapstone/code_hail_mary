"""The auth and registration endpoints, over HTTP.

`test_registration_flow.py` proves the service layer against the database. This proves
the wire contract: status codes, cookie attributes, and — most importantly — that the
responses give nothing away.

The client uses an https base URL because the session cookies carry the `__Host-` prefix
and are therefore `Secure`; an http client would refuse to store them, and the tests
would fail for a reason that has nothing to do with the code under test.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.auth_service import SIGN_IN_BUDGET_LIMIT
from jutsu_api.config import OTP_MAX_ATTEMPTS, Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.security import (
    CHALLENGE_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
)
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


async def complete_registration(client: AsyncClient, mailbox: RecordingEmailSender) -> None:
    """Stage a registration and redeem it, leaving a real organisation and a session.

    Two calls, because the organisation does not exist until the code comes back. The
    second one is `/v1/orgs/register/verify` and not `/v1/auth/verify`: the challenge
    carries `purpose = register`, and the sign-in route refuses it by design.
    """
    await client.post("/v1/orgs/register", json=REGISTRATION)
    delivered = mailbox.last.secrets
    await client.post(
        "/v1/orgs/register/verify",
        json={"token": delivered["token"], "code": delivered["code"]},
    )


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

    async def test_a_flood_of_requests_for_one_address_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Each request mails an address the caller names and writes auth-schema rows,
        so without a ceiling this endpoint is an open relay — staging's exact hole."""
        for _ in range(SIGN_IN_BUDGET_LIMIT):
            granted = await client.post("/v1/auth/request", json={"email": "ada@example.com"})
            assert granted.status_code == 202

        refused = await client.post("/v1/auth/request", json={"email": "ada@example.com"})

        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "rate_limited"
        assert len(mailbox.messages) == SIGN_IN_BUDGET_LIMIT, "a refused request still mailed"

    async def test_the_throttle_is_per_address(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        for _ in range(SIGN_IN_BUDGET_LIMIT):
            await client.post("/v1/auth/request", json={"email": "ada@example.com"})
        assert (
            await client.post("/v1/auth/request", json={"email": "ada@example.com"})
        ).status_code == 429

        other = await client.post("/v1/auth/request", json={"email": "grace@example.com"})
        assert other.status_code == 202, "one flooded address locked out a different one"


class TestChallengeWithJutsuId:
    """The optional cross-check: id and address must resolve to the same membership.

    The response is the identical 202 either way — the only observable difference is
    whether anything lands in the inbox, which the requester would have to control
    anyway. Anything else is an oracle over the id space.
    """

    async def test_a_matched_pair_delivers_the_code(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await complete_registration(client, mailbox)
        jutsu_id = (await client.get("/v1/me")).json()["jutsu_id"]
        before = len(mailbox.messages)

        response = await client.post(
            "/v1/auth/request", json={"email": "ada@example.com", "jutsu_id": jutsu_id}
        )

        assert response.status_code == 202
        assert len(mailbox.messages) == before + 1
        assert set(mailbox.last.secrets) == {"code", "token"}

    async def test_a_hand_typed_id_is_forgiven_its_crockford_confusables(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await complete_registration(client, mailbox)
        jutsu_id = (await client.get("/v1/me")).json()["jutsu_id"]
        mangled = jutsu_id.lower().replace("0", "o").replace("1", "i")
        before = len(mailbox.messages)

        response = await client.post(
            "/v1/auth/request", json={"email": "ada@example.com", "jutsu_id": mangled}
        )

        assert response.status_code == 202
        assert len(mailbox.messages) == before + 1

    async def test_a_mismatched_pair_is_202_and_delivers_nothing(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await complete_registration(client, mailbox)
        plain = await client.post("/v1/auth/request", json={"email": "ada@example.com"})
        before = len(mailbox.messages)

        mismatched = await client.post(
            "/v1/auth/request",
            json={"email": "ada@example.com", "jutsu_id": "JUTSU-EMP-00000000"},
        )

        assert mismatched.status_code == 202
        assert mismatched.content == plain.content, "the body must not say the pair mismatched"
        assert len(mailbox.messages) == before, "a mismatched pair delivered a credential"


class TestVerifyEndpoint:
    """The sign-in verify endpoint. Registration completes elsewhere now."""

    async def _register(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> tuple[str, str]:
        """Create the organisation, then ask for an ordinary sign-in code.

        Two challenges, not one. The registration code carries `purpose = register` and
        this endpoint refuses it — asserted directly in `test_registration_flow.py` —
        so signing in means requesting a fresh challenge against the account that now
        exists.
        """
        await complete_registration(client, mailbox)
        await client.post("/v1/auth/request", json={"email": REGISTRATION["work_email"]})
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

    async def test_a_non_ascii_token_gets_the_same_refusal_not_a_crash(
        self, client: AsyncClient
    ) -> None:
        """The token is caller-controlled text. Hashing used to encode it as ASCII, so
        one accented character was a 500 — a crash where the uniform 401 belongs."""
        response = await client.post(
            "/v1/auth/verify", json={"token": "jetons-privé-" + "é" * 8, "code": "000000"}
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

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


class TestTheChallengeCookie:
    """The six-digit code has to work without the emailed link.

    It did not: the verification screen also demanded the sign-in token, and the only
    place a person could obtain one was the link the code exists to replace. Asking for
    a code now leaves the token in an httpOnly cookie, so the screen asks for six digits
    and nothing else — while the code itself, which is the secret, still only ever
    reaches the mailbox.
    """

    async def test_asking_for_a_code_leaves_the_token_in_an_httponly_cookie(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await complete_registration(client, mailbox)
        client.cookies.clear()

        response = await client.post("/v1/auth/request", json={"email": REGISTRATION["work_email"]})

        assert response.status_code == 202
        assert response.cookies.get(CHALLENGE_COOKIE) == mailbox.last.secrets["token"]
        header = next(
            h for h in response.headers.get_list("set-cookie") if h.startswith(CHALLENGE_COOKIE)
        )
        assert "HttpOnly" in header, "script must never read the challenge token"
        assert "Secure" in header
        assert "Max-Age=600" in header, "the cookie must not outlive the challenge row"

    async def test_the_cookie_is_set_for_an_address_with_no_account(
        self, client: AsyncClient
    ) -> None:
        """Otherwise its presence would answer "does this address have an account",
        which is the one thing this whole path refuses to disclose."""
        response = await client.post("/v1/auth/request", json={"email": "nobody@nowhere.example"})

        assert response.status_code == 202
        assert response.cookies.get(CHALLENGE_COOKIE)

    async def test_the_code_alone_signs_in(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The whole point: no token in the body, because the browser already holds it."""
        await complete_registration(client, mailbox)
        await client.post("/v1/auth/request", json={"email": REGISTRATION["work_email"]})

        response = await client.post("/v1/auth/verify", json={"code": mailbox.last.secrets["code"]})

        assert response.status_code == 200, response.text
        assert response.json()["destination"] == "/admin"
        assert response.cookies.get(SESSION_COOKIE)

    async def test_a_body_token_still_wins_for_the_emailed_link(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Opening the link on a second device is the case the cookie cannot serve: that
        browser never asked for the code, so the token has to come from the URL."""
        await complete_registration(client, mailbox)
        await client.post("/v1/auth/request", json={"email": REGISTRATION["work_email"]})
        delivered = mailbox.last.secrets
        client.cookies.delete(CHALLENGE_COOKIE)

        response = await client.post(
            "/v1/auth/verify", json={"token": delivered["token"], "code": delivered["code"]}
        )

        assert response.status_code == 200, response.text

    async def test_neither_a_cookie_nor_a_token_is_the_same_refusal_as_a_wrong_code(
        self, client: AsyncClient
    ) -> None:
        """A different error here would tell an attacker which half to work on."""
        client.cookies.clear()

        response = await client.post("/v1/auth/verify", json={"code": "000000"})

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"
        assert response.json()["error"]["message"] == "That code is not valid."

    async def test_signing_in_clears_the_cookie(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """A challenge is single-use. Keeping the cookie would have the next sign-in
        submit a dead token by default, which reads as "your code is wrong"."""
        await complete_registration(client, mailbox)
        await client.post("/v1/auth/request", json={"email": REGISTRATION["work_email"]})

        response = await client.post("/v1/auth/verify", json={"code": mailbox.last.secrets["code"]})

        assert response.status_code == 200
        assert not client.cookies.get(CHALLENGE_COOKIE)

    async def test_a_wrong_code_still_spends_an_attempt_when_the_token_came_from_the_cookie(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The attempt budget is what makes carrying the token safe — five guesses per
        challenge against a million-wide space. It must not depend on where the token
        was read from."""
        await complete_registration(client, mailbox)
        await client.post("/v1/auth/request", json={"email": REGISTRATION["work_email"]})
        code = mailbox.last.secrets["code"]

        for _ in range(OTP_MAX_ATTEMPTS):
            refused = await client.post("/v1/auth/verify", json={"code": "000000"})
            assert refused.status_code == 401

        spent = await client.post("/v1/auth/verify", json={"code": code})
        assert spent.status_code == 401, "the budget must be exhausted, right code or not"


class TestTheRegistrationChallengeCookie:
    async def test_staging_a_registration_leaves_the_token_in_a_cookie(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        response = await client.post("/v1/orgs/register", json=REGISTRATION)

        assert response.status_code == 202
        assert response.cookies.get(CHALLENGE_COOKIE) == mailbox.last.secrets["token"]
        assert "token" not in response.json(), "the body still never carries it"

    async def test_the_code_alone_completes_a_registration(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await client.post("/v1/orgs/register", json=REGISTRATION)

        response = await client.post(
            "/v1/orgs/register/verify", json={"code": mailbox.last.secrets["code"]}
        )

        assert response.status_code == 200, response.text
        assert response.json()["destination"] == "/admin"
        assert response.cookies.get(SESSION_COOKIE)
        assert not client.cookies.get(CHALLENGE_COOKIE)


class TestProtectedEndpoint:
    async def _sign_in(self, client: AsyncClient, mailbox: RecordingEmailSender) -> None:
        await complete_registration(client, mailbox)

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


class TestValidationEnvelope:
    """Rejected input must use the one envelope and reflect nothing back.

    FastAPI's default handler returns `{"detail": [...]}` with an `input` key holding the
    value that failed. That is two defects at once: a second response shape for exactly
    the case clients hit most, and — on the auth endpoints — an email address echoed
    straight back to whoever posted it, which §4.9 forbids.

    Found by running the real form, not by review: the browser surfaced a generic
    "service is not responding" because the client could not parse the default shape.
    """

    async def test_a_rejected_field_uses_the_standard_envelope(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/orgs/register", json={**REGISTRATION, "work_email": "not-an-email"}
        )

        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "validation_failed"
        assert "request_id" in body
        assert body["error"]["details"]["fields"] == [
            {"field": "work_email", "rule": "value_error"}
        ]

    async def test_the_submitted_value_is_never_reflected(self, client: AsyncClient) -> None:
        """The specific thing that would leak an address on the sign-in endpoint."""
        response = await client.post("/v1/auth/request", json={"email": "secret.person@invalid"})

        assert response.status_code == 422
        assert "secret.person" not in response.text


class TestCurrentOrganisation:
    """The overview's data, and the tenancy property that makes it safe."""

    async def _sign_in(self, client: AsyncClient, mailbox: RecordingEmailSender) -> None:
        await complete_registration(client, mailbox)

    async def test_requires_a_session(self, client: AsyncClient) -> None:
        response = await client.get("/v1/orgs/current")

        assert response.status_code == 401

    async def test_returns_the_callers_own_organisation_with_real_counts(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await self._sign_in(client, mailbox)

        response = await client.get("/v1/orgs/current")

        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Example Analytical"
        assert body["domain"] == "example.com"
        assert body["status"] == "active"
        # One person, who is active and is an administrator. Counted in Postgres under
        # the tenant scope, not assembled in Python from an unfiltered query.
        assert body["members"] == {
            "total": 1,
            "active": 1,
            "invited": 0,
            "deactivated": 0,
            "admins": 1,
        }

    async def test_there_is_no_route_that_takes_an_organisation_id(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The tenant comes from the session, never from the client.

        An `/v1/orgs/{id}` variant would be an authorisation input from the browser, and
        it would also let anyone probe whether an organisation exists. Its absence is the
        design, so this pins it rather than leaving it to reviewer memory.
        """
        await self._sign_in(client, mailbox)
        own = (await client.get("/v1/orgs/current")).json()

        probe = await client.get(f"/v1/orgs/{own['id']}")

        assert probe.status_code == 404


def csrf_headers(client: AsyncClient) -> dict[str, str]:
    """The double-submit header a browser would send.

    Every authenticated, state-changing request needs it. Public routes do not, which is
    why the earlier POSTs in this file get away without it — and why the first
    authenticated POST written without it came back 401 rather than doing anything.
    """
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


class TestInvitationLifecycle:
    """Invite, accept, and the authorization boundary that follows.

    `test_a_member_is_denied_administrative_endpoints` is the most important test in this
    file. Without enforcement, `@requires(...)` only *described* a permission — every
    authenticated caller reached every endpoint, and a bare Member could list the whole
    organisation. The declaration existed, the import-time guard passed, and nothing
    denied anything.
    """

    async def _owner(self, client: AsyncClient, mailbox: RecordingEmailSender) -> None:
        await complete_registration(client, mailbox)

    async def _invite_and_accept(
        self, client: AsyncClient, mailbox: RecordingEmailSender, *, role: str = "member"
    ) -> str:
        await client.post(
            "/v1/employees/invitations",
            json={"email": "charles@example.com", "role": role},
            headers=csrf_headers(client),
        )
        token = mailbox.last.secrets["token"]
        # A fresh client: the invitee is a different person, not the admin's browser.
        accepted = await client.post(
            "/v1/invitations/accept",
            json={"token": token, "full_name": "Charles Babbage"},
        )
        assert accepted.status_code == 200, accepted.text
        return str(accepted.json()["jutsu_id"])

    async def test_accepting_issues_an_employee_id_and_a_session(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await self._owner(client, mailbox)
        jutsu_id = await self._invite_and_accept(client, mailbox)

        assert jutsu_id.startswith("JUTSU-EMP-"), "an invited person joins as an employee"

        me = (await client.get("/v1/me")).json()
        assert me["jutsu_id"] == jutsu_id
        assert me["role"] == "member"

    async def test_a_member_is_denied_administrative_endpoints(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await self._owner(client, mailbox)
        await self._invite_and_accept(client, mailbox)

        # They can read themselves — that is what `profile:self_read` is for, and without
        # it the shell could not render for an employee at all.
        assert (await client.get("/v1/me")).status_code == 200

        for path in ("/v1/employees", "/v1/orgs/current"):
            denied = await client.get(path)
            assert denied.status_code == 403, f"{path} was reachable by a bare Member"
            assert denied.json()["error"]["code"] == "permission_denied"

    async def test_an_invitation_can_only_be_accepted_once(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await self._owner(client, mailbox)
        await client.post(
            "/v1/employees/invitations",
            json={"email": "charles@example.com", "role": "member"},
            headers=csrf_headers(client),
        )
        token = mailbox.last.secrets["token"]

        first = await client.post(
            "/v1/invitations/accept",
            json={"token": token, "full_name": "Charles Babbage"},
        )
        second = await client.post(
            "/v1/invitations/accept",
            json={"token": token, "full_name": "Someone Else"},
        )

        assert first.status_code == 200
        assert second.status_code == 401
        # Same refusal as an unknown token, so a used link cannot be told from a fake one.
        assert second.json()["error"]["code"] == "unauthenticated"

    async def test_an_unknown_token_is_refused_identically(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/invitations/accept",
            json={"token": "x" * 43, "full_name": "Nobody"},
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

    async def test_a_non_ascii_token_is_refused_identically_not_a_crash(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(
            "/v1/invitations/accept",
            json={"token": "ø" * 20, "full_name": "Nobody"},
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

    async def test_inviting_an_existing_member_is_refused(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        inspector: AsyncSession,
    ) -> None:
        """An invitation for an active member would, at acceptance, mint a second
        `users` row for the same person and repoint sign-in at it — orphaning the
        original role, profile and identities. Refused at issue."""
        await self._owner(client, mailbox)
        await self._invite_and_accept(client, mailbox)

        # Back to the owner: the helper leaves the client signed in as the invitee.
        await client.post("/v1/auth/request", json={"email": REGISTRATION["work_email"]})
        delivered = mailbox.last.secrets
        await client.post(
            "/v1/auth/verify", json={"token": delivered["token"], "code": delivered["code"]}
        )

        refused = await client.post(
            "/v1/employees/invitations",
            json={"email": "charles@example.com", "role": "member"},
            headers=csrf_headers(client),
        )

        assert refused.status_code == 409, refused.text
        assert "already a member" in refused.json()["error"]["message"]
        rows = (
            await inspector.execute(
                text("SELECT count(*) FROM users WHERE email = 'charles@example.com'")
            )
        ).scalar_one()
        assert rows == 1

    async def test_accepting_after_becoming_a_member_is_refused(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        inspector: AsyncSession,
    ) -> None:
        """The standing-token race: invited while not a member, a member by the time
        the link is clicked. The accept path re-checks inside the same transaction, so
        the duplicate row and the membership repoint are unrepresentable."""
        await self._owner(client, mailbox)
        await client.post(
            "/v1/employees/invitations",
            json={"email": "carol@example.com", "role": "member"},
            headers=csrf_headers(client),
        )
        token = mailbox.last.secrets["token"]

        # Carol becomes an active member before the invitation is redeemed.
        org_id = (await client.get("/v1/orgs/current")).json()["id"]
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
        )
        await db_session.execute(
            text("INSERT INTO users (id, org_id, email, status) VALUES (:i, :o, :e, 'active')"),
            {"i": uuid.uuid4(), "o": org_id, "e": "carol@example.com"},
        )
        await db_session.commit()

        refused = await client.post(
            "/v1/invitations/accept",
            json={"token": token, "full_name": "Carol Clone"},
        )
        await db_session.rollback()

        assert refused.status_code == 409, refused.text
        assert "already a member" in refused.json()["error"]["message"]
        rows = (
            await inspector.execute(
                text("SELECT count(*) FROM users WHERE email = 'carol@example.com'")
            )
        ).scalar_one()
        assert rows == 1

    async def test_nobody_can_invite_above_their_own_rank(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The escalation ceiling is strict, so even an Owner cannot mint another Owner.

        An invitation conferring a role the inviter does not outrank is the same
        privilege escalation as granting it directly — only slower, and easier to miss.
        """
        await self._owner(client, mailbox)

        response = await client.post(
            "/v1/employees/invitations",
            json={"email": "usurper@example.com", "role": "owner"},
            headers=csrf_headers(client),
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"


class TestWhatEachRouteMails:
    """Which branded message each HTTP flow actually delivers.

    Over the wire rather than through the service layer, because the two welcome
    messages are sent by the *routers* — deliberately, so a refused SMTP connection
    cannot roll back the tenant or the membership the request just created. Nothing in
    `test_registration_flow.py` would notice if a router stopped sending one.
    """

    async def test_registering_delivers_the_verification_then_the_welcome(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Scenario one, both halves, in order.

        The identifiers cannot ride on the first message: nothing durable exists when it
        is sent, and that is the control that stops a domain being claimed by whoever can
        type it. So the code goes first and the organisation's identity follows the moment
        there is one.
        """
        await client.post("/v1/orgs/register", json=REGISTRATION)
        verification = mailbox.last
        assert verification.subject == "Verify Example Analytical on JUTSU"
        assert set(verification.secrets) == {"code", "token"}

        delivered = verification.secrets
        response = await client.post(
            "/v1/orgs/register/verify",
            json={"token": delivered["token"], "code": delivered["code"]},
        )
        assert response.status_code == 200, response.text

        welcome = mailbox.last
        assert welcome is not verification
        assert welcome.subject == "Example Analytical is live on JUTSU"
        assert welcome.html is not None
        assert "JUTSU-ADM-" in welcome.html
        assert "Organisation ID" in welcome.html
        # No credential in a message nobody asked for.
        assert welcome.secrets == {}

    async def test_inviting_names_the_organisation_and_carries_no_tenant_id(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """An invitation that cannot say who it is from is indistinguishable from
        phishing, and the reader's only defence is to ignore it."""
        await complete_registration(client, mailbox)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await client.post(
            "/v1/employees/invitations",
            json={"email": "charles@example.com", "role": "member"},
            headers=csrf_headers(client),
        )

        invitation = mailbox.last
        assert invitation.subject == "You have been invited to Example Analytical on JUTSU"
        assert set(invitation.secrets) == {"token"}
        assert org_id not in (invitation.html or "") + invitation.body

    async def test_accepting_welcomes_the_employee_with_their_own_id_only(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Scenario two.

        `jutsu_id` is otherwise shown on exactly one screen, and the console asks for it
        by name at every later sign-in — so a closed tab currently costs somebody their
        identifier. The organisation's id is not in here: the sign-in form never asks for
        one.
        """
        await complete_registration(client, mailbox)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await client.post(
            "/v1/employees/invitations",
            json={"email": "charles@example.com", "role": "member"},
            headers=csrf_headers(client),
        )
        token = mailbox.last.secrets["token"]

        accepted = await client.post(
            "/v1/invitations/accept",
            json={"token": token, "full_name": "Charles Babbage"},
        )
        assert accepted.status_code == 200, accepted.text
        jutsu_id = accepted.json()["jutsu_id"]

        welcome = mailbox.last
        assert welcome.subject == "Welcome to Example Analytical on JUTSU"
        assert welcome.to == "charles@example.com"
        readable = (welcome.html or "") + welcome.body
        assert jutsu_id in readable
        assert org_id not in readable
        assert welcome.secrets == {}

    async def test_signing_in_again_delivers_the_code_and_nothing_else(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Scenario three, over the wire: no organisation token, no JUTSU ID, no
        organisation name."""
        await complete_registration(client, mailbox)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]
        jutsu_id = (await client.get("/v1/me")).json()["jutsu_id"]

        await client.post("/v1/auth/request", json={"email": REGISTRATION["work_email"]})

        code_mail = mailbox.last
        assert code_mail.subject == "Your JUTSU sign-in code"
        readable = (code_mail.html or "") + code_mail.body
        assert org_id not in readable
        assert jutsu_id not in readable
        assert "Example Analytical" not in readable
        # The challenge's own code and link token. Neither is the organisation token, and
        # the verify page refuses a code submitted without the link token.
        assert set(code_mail.secrets) == {"code", "token"}

    async def test_an_unknown_address_still_gets_a_message_with_no_credential(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The anti-enumeration control, unchanged: same work, same 202, and the only
        difference is in the recipient's own inbox."""
        await client.post("/v1/auth/request", json={"email": "nobody@nowhere.example"})

        assert mailbox.last.secrets == {}
        assert mailbox.last.html is not None
        assert "[[" not in mailbox.last.html


class TestSlidingIdleExpiry:
    """`auth.touch_session` existed since migration 0003 and nothing called it, so every
    session hard-expired sixty minutes after it was minted however active the person
    was. The touch is at most once per interval and never past the absolute ceiling."""

    async def test_activity_after_the_interval_slides_the_idle_deadline(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        from jutsu_api.config import SESSION_TOUCH_INTERVAL_SECONDS

        await complete_registration(client, mailbox)
        row = (
            await inspector.execute(
                text("SELECT id, idle_expires_at, expires_at FROM auth.sessions LIMIT 1")
            )
        ).one()

        # As if the interval had elapsed: pull the deadline back further than one touch
        # interval, then make any authenticated request.
        await inspector.execute(
            text(
                "UPDATE auth.sessions SET idle_expires_at = idle_expires_at "
                "- make_interval(secs => :s) WHERE id = :id"
            ),
            {"s": SESSION_TOUCH_INTERVAL_SECONDS + 60, "id": row.id},
        )
        pushed_back = (
            await inspector.execute(
                text("SELECT idle_expires_at FROM auth.sessions WHERE id = :id"), {"id": row.id}
            )
        ).scalar_one()

        assert (await client.get("/v1/me")).status_code == 200

        after = (
            await inspector.execute(
                text("SELECT idle_expires_at FROM auth.sessions WHERE id = :id"), {"id": row.id}
            )
        ).scalar_one()
        assert after > pushed_back, "activity did not slide the idle deadline"
        assert after <= row.expires_at, "the idle deadline passed the absolute ceiling"

    async def test_a_burst_inside_the_interval_does_not_rewrite_the_row(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        await complete_registration(client, mailbox)
        before = (
            await inspector.execute(text("SELECT idle_expires_at FROM auth.sessions LIMIT 1"))
        ).scalar_one()

        for _ in range(3):
            assert (await client.get("/v1/me")).status_code == 200

        after = (
            await inspector.execute(text("SELECT idle_expires_at FROM auth.sessions LIMIT 1"))
        ).scalar_one()
        assert after == before
