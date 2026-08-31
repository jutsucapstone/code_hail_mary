"""The connection lifecycle, over the wire, against real Postgres and RLS.

What must hold, in rough order of how expensive a failure would be:

* No response anywhere in the flow carries a token, in any spelling.
* The OAuth callback binds to the caller: someone else's `state` is a 404, and a spent
  `state` is a 404 — replayed callbacks find nothing.
* Disconnecting is owner-only; a colleague inside the same organisation gets a 404.
* Administrative revocation obeys the rank rules and writes an audit row.
* Policy denial refuses the flow before anything provider-shaped happens.
* An unconfigured provider is an honest 503, never a pretend success.

The provider round trip is a fake injected through the same seam the email sender uses.
The FAKE is what makes "no fake OAuth" testable: the flow is real up to the seam, and
the seam's contract is exactly two provider calls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.connectors import (
    PROVIDERS,
    AccountIdentity,
    OAuthClient,
    Provider,
    TokenGrant,
)
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.routers.connections import get_oauth_transport
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

#: What the fake provider hands back. The value is deliberately conspicuous so the
#: no-token-leak assertions can grep responses for it.
ACCESS_TOKEN = "tok-access-SENTINEL-do-not-serve"
REFRESH_TOKEN = "tok-refresh-SENTINEL-do-not-serve"


class FakeOAuth:
    """The two provider calls, recorded. Raising versions live in the tests that need
    them."""

    def __init__(self) -> None:
        self.exchanges: list[str] = []

    async def exchange_code(
        self, provider: Provider, client: OAuthClient, *, code: str, redirect_uri: str
    ) -> TokenGrant:
        self.exchanges.append(code)
        return TokenGrant(access_token=ACCESS_TOKEN, refresh_token=REFRESH_TOKEN, expires_in=3600)

    async def fetch_identity(self, provider: Provider, access_token: str) -> AccountIdentity:
        return AccountIdentity(subject="U0AB12CD", label="ada@slack.example")


@pytest.fixture
def oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured deployment: slack credentials plus an encryption key."""
    monkeypatch.setenv("JUTSU_OAUTH_SLACK_CLIENT_ID", "slack-client-id")
    monkeypatch.setenv("JUTSU_OAUTH_SLACK_CLIENT_SECRET", "slack-client-secret")
    monkeypatch.setenv("JUTSU_CONNECTION_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture
def fake_oauth() -> FakeOAuth:
    return FakeOAuth()


@pytest.fixture
async def client(
    db_session: AsyncSession,
    settings: Settings,
    mailbox: RecordingEmailSender,
    fake_oauth: FakeOAuth,
) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox
    app.dependency_overrides[get_oauth_transport] = lambda: fake_oauth

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
        "/v1/invitations/accept", json={"token": token, "full_name": "Someone"}
    )
    assert accepted.status_code == 200, accepted.text


async def sign_in(client: AsyncClient, mailbox: RecordingEmailSender, *, email: str) -> None:
    await client.post("/v1/auth/request", json={"email": email})
    delivered = mailbox.last.secrets
    verified = await client.post(
        "/v1/auth/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text


async def connect_slack(client: AsyncClient) -> tuple[str, str]:
    """Start a slack flow; return (connection_id, state) with state read from the URL."""
    started = await client.post("/v1/me/connections/slack", headers=csrf(client))
    assert started.status_code == 201, started.text
    body = started.json()
    authorize_url = body["authorize_url"]
    state = authorize_url.split("state=")[1].split("&")[0]
    return body["connection_id"], state


async def complete(client: AsyncClient, state: str) -> object:
    return await client.get(
        "/v1/connections/callback",
        params={"state": state, "code": "provider-code-1"},
        follow_redirects=False,
    )


# --------------------------------------------------------------------------------------
# Catalogue honesty
# --------------------------------------------------------------------------------------


class TestCatalogue:
    async def test_unconfigured_providers_say_so(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """No credentials in the environment → configured: false, for every provider."""
        await register_owner(client, mailbox)

        body = (await client.get("/v1/integrations")).json()
        assert len(body["items"]) == len(PROVIDERS)
        assert all(item["configured"] is False for item in body["items"])
        assert all(item["connection"] is None for item in body["items"])

    async def test_connecting_an_unconfigured_provider_is_a_503_not_a_fake_success(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)

        response = await client.post("/v1/me/connections/github", headers=csrf(client))
        assert response.status_code == 503
        assert "not configured" in response.json()["error"]["message"]

    async def test_an_unknown_provider_is_a_404(
        self, client: AsyncClient, mailbox: RecordingEmailSender, oauth_env: None
    ) -> None:
        await register_owner(client, mailbox)
        response = await client.post("/v1/me/connections/napster", headers=csrf(client))
        assert response.status_code == 404


# --------------------------------------------------------------------------------------
# The flow itself
# --------------------------------------------------------------------------------------


class TestConnectFlow:
    async def test_the_full_round_trip_connects(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        fake_oauth: FakeOAuth,
    ) -> None:
        await register_owner(client, mailbox)
        _, state = await connect_slack(client)

        callback = await complete(client, state)
        assert callback.status_code == 303  # type: ignore[attr-defined]
        assert fake_oauth.exchanges == ["provider-code-1"]

        body = (await client.get("/v1/integrations")).json()
        slack = next(item for item in body["items"] if item["id"] == "slack")
        assert slack["connection"]["status"] == "connected"
        assert slack["connection"]["account_label"] == "ada@slack.example"

    async def test_no_response_in_the_flow_ever_carries_a_token(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
    ) -> None:
        """The single most important property on this surface (§46)."""
        await register_owner(client, mailbox)
        started = await client.post("/v1/me/connections/slack", headers=csrf(client))
        state = started.json()["authorize_url"].split("state=")[1].split("&")[0]
        callback = await complete(client, state)
        catalogue = await client.get("/v1/integrations")
        summary = await client.get("/v1/connections/summary")

        for response in (started, callback, catalogue, summary):
            assert "SENTINEL" not in response.text  # type: ignore[attr-defined]
            assert "tok-access" not in response.text  # type: ignore[attr-defined]
            assert "tok-refresh" not in response.text  # type: ignore[attr-defined]

    async def test_tokens_are_stored_encrypted_never_plaintext(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        inspector: AsyncSession,
    ) -> None:
        """Even the privileged inspector must find only ciphertext."""
        await register_owner(client, mailbox)
        _, state = await connect_slack(client)
        await complete(client, state)

        row = (
            await inspector.execute(
                text("SELECT access_token_enc, refresh_token_enc FROM connection_credentials")
            )
        ).one()
        assert ACCESS_TOKEN.encode() not in row.access_token_enc
        assert REFRESH_TOKEN.encode() not in row.refresh_token_enc

    async def test_a_spent_state_cannot_be_replayed(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        fake_oauth: FakeOAuth,
    ) -> None:
        await register_owner(client, mailbox)
        _, state = await connect_slack(client)
        first = await complete(client, state)
        assert first.status_code == 303  # type: ignore[attr-defined]

        replay = await complete(client, state)
        assert replay.status_code == 404  # type: ignore[attr-defined]
        assert fake_oauth.exchanges == ["provider-code-1"], "the replay must not reach the provider"

    async def test_someone_elses_state_is_a_404(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        fake_oauth: FakeOAuth,
    ) -> None:
        """The state binds the callback to the session that started the flow."""
        await register_owner(client, mailbox)
        _, state = await connect_slack(client)

        # A colleague's session arrives carrying the owner's state.
        await invite_and_accept(client, mailbox, email="mallory@example.com")
        stolen = await complete(client, state)
        assert stolen.status_code == 404  # type: ignore[attr-defined]
        assert fake_oauth.exchanges == [], "nothing may reach the provider for a stolen state"

    async def test_policy_denial_refuses_before_anything_starts(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
    ) -> None:
        await register_owner(client, mailbox)
        denied = await client.put(
            "/v1/connection-policies/slack", json={"allowed": False}, headers=csrf(client)
        )
        assert denied.status_code == 200

        response = await client.post("/v1/me/connections/slack", headers=csrf(client))
        assert response.status_code == 403
        assert "does not allow" in response.json()["error"]["message"]


# --------------------------------------------------------------------------------------
# Disconnection and revocation
# --------------------------------------------------------------------------------------


class TestDisconnectAndRevoke:
    async def test_owner_disconnects_their_own(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
    ) -> None:
        await register_owner(client, mailbox)
        connection_id, state = await connect_slack(client)
        await complete(client, state)

        response = await client.delete(f"/v1/me/connections/{connection_id}", headers=csrf(client))
        assert response.status_code == 204

        body = (await client.get("/v1/integrations")).json()
        slack = next(item for item in body["items"] if item["id"] == "slack")
        assert slack["connection"] is None

    async def test_a_colleague_cannot_disconnect_it(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
    ) -> None:
        """RLS scopes the organisation; the ownership check is what protects the person."""
        await register_owner(client, mailbox)
        connection_id, state = await connect_slack(client)
        await complete(client, state)

        await invite_and_accept(client, mailbox, email="colleague@example.com")
        response = await client.delete(f"/v1/me/connections/{connection_id}", headers=csrf(client))
        assert response.status_code == 404

    async def test_disconnecting_deletes_the_credentials(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        inspector: AsyncSession,
    ) -> None:
        await register_owner(client, mailbox)
        connection_id, state = await connect_slack(client)
        await complete(client, state)
        await client.delete(f"/v1/me/connections/{connection_id}", headers=csrf(client))

        remaining = (
            await inspector.execute(text("SELECT count(*) FROM connection_credentials"))
        ).scalar_one()
        assert remaining == 0, "a disconnected connection must not keep a usable credential"

    async def test_reconnecting_after_disconnect_is_allowed(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
    ) -> None:
        """History must not block a fresh start — the unique index is partial."""
        await register_owner(client, mailbox)
        connection_id, state = await connect_slack(client)
        await complete(client, state)
        await client.delete(f"/v1/me/connections/{connection_id}", headers=csrf(client))

        again = await client.post("/v1/me/connections/slack", headers=csrf(client))
        assert again.status_code == 201

    async def test_an_admin_revokes_a_members_connection_and_it_is_audited(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")
        connection_id, state = await connect_slack(client)
        await complete(client, state)

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        response = await client.delete(f"/v1/connections/{connection_id}", headers=csrf(client))
        assert response.status_code == 204

        trail = (await client.get("/v1/audit", params={"action": "connection.revoked"})).json()
        assert len(trail["items"]) == 1

    async def test_an_hr_admin_cannot_revoke_an_it_admins_connection(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
    ) -> None:
        """Equal ranks, disjoint powers — the same rule as identities and roles."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="it@example.com", role="it_admin")
        connection_id, state = await connect_slack(client)
        await complete(client, state)

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await invite_and_accept(client, mailbox, email="hr@example.com", role="hr_admin")
        await sign_in(client, mailbox, email="hr@example.com")

        response = await client.delete(f"/v1/connections/{connection_id}", headers=csrf(client))
        assert response.status_code == 403

    async def test_a_member_cannot_reach_the_admin_surface_at_all(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")

        assert (await client.get("/v1/connections/summary")).status_code == 403
        assert (await client.get("/v1/connection-policies")).status_code == 403


# --------------------------------------------------------------------------------------
# Governance
# --------------------------------------------------------------------------------------


class TestGovernance:
    async def test_summary_counts_without_identities(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
    ) -> None:
        await register_owner(client, mailbox)
        _, state = await connect_slack(client)
        await complete(client, state)

        response = await client.get("/v1/connections/summary")
        body = response.json()
        assert body["items"] == [
            {"provider": "slack", "name": "Slack", "total": 1, "by_status": {"connected": 1}}
        ]
        # Aggregates carry no account identity anywhere.
        assert "ada@slack.example" not in response.text

    async def test_policy_change_is_audited(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
    ) -> None:
        await register_owner(client, mailbox)
        await client.put(
            "/v1/connection-policies/github", json={"allowed": False}, headers=csrf(client)
        )

        trail = (
            await client.get("/v1/audit", params={"action": "connection.policy_changed"})
        ).json()
        assert len(trail["items"]) == 1
        assert trail["items"][0]["resource_id"] == "github"

    async def test_every_registry_provider_fits_the_check_constraint(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
    ) -> None:
        """The registry and migration 0012's CHECK list cannot drift apart silently."""
        await register_owner(client, mailbox)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]
        me = (await client.get("/v1/me")).json()

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
        )
        for provider_id in PROVIDERS:
            # An id missing from the CHECK raises here, naming the drift exactly.
            await db_session.execute(
                text(
                    "INSERT INTO connections (id, org_id, user_id, provider, status) "
                    "VALUES (gen_random_uuid(), :org, :user, :provider, 'disconnected')"
                ),
                {"org": org_id, "user": me["user_id"], "provider": provider_id},
            )
