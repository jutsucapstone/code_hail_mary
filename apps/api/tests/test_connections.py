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

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.connectors import (
    PROVIDERS,
    AccountIdentity,
    HttpOAuthTransport,
    OAuthClient,
    Provider,
    TokenGrant,
    start_connection,
)
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.routers.connections import get_oauth_transport
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from jutsu_core.errors import Conflict, ServiceUnavailable
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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
    """The provider calls, recorded. Raising versions live in the tests that need
    them."""

    def __init__(self) -> None:
        self.exchanges: list[str] = []
        self.verifiers: list[str | None] = []
        self.revoked: list[str] = []

    async def exchange_code(
        self,
        provider: Provider,
        client: OAuthClient,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> TokenGrant:
        self.exchanges.append(code)
        self.verifiers.append(code_verifier)
        return TokenGrant(access_token=ACCESS_TOKEN, refresh_token=REFRESH_TOKEN, expires_in=3600)

    async def fetch_identity(self, provider: Provider, access_token: str) -> AccountIdentity:
        return AccountIdentity(subject="U0AB12CD", label="ada@slack.example")

    async def revoke_upstream(
        self, provider: Provider, client: OAuthClient, *, access_token: str
    ) -> None:
        self.revoked.append(access_token)


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
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """The app, plus what the callback's own-session spend forces this fixture to own.

    `complete_callback` spends the OAuth state on its own committed session through
    `jutsu_db.engine` — the rate limiter's design, for the rate limiter's reason — so
    exactly as in `test_search_api`: `DATABASE_URL` must point at the application role
    (`db_session` set it to the privileged migration URL for Alembic, under which RLS
    is silently inert), and the process-wide engine cache must be disposed around every
    test or a later test inherits a pool bound to a closed event loop.
    """
    from jutsu_db.engine import dispose_engine

    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", database_url)

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
        http.app = app  # type: ignore[attr-defined]
        yield http

    await dispose_engine()


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

    async def test_document_count_measures_current_documents_of_the_connections_source(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        db_session: AsyncSession,
    ) -> None:
        """Pins the join key: `sources.config_json->>'connection_id' = connections.id`.

        The count is §2's "indexed item counts where available" — measured, so this
        seeds the measurement: a source naming the connection, one current document,
        one superseded version that is history and must not count.
        """
        await register_owner(client, mailbox)
        connection_id, state = await connect_slack(client)
        await complete(client, state)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
        )
        source_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json, status) "
                "VALUES (:id, :org, 'slack', cast(:config AS jsonb), 'idle')"
            ),
            {
                "id": source_id,
                "org": org_id,
                "config": f'{{"connection_id": "{connection_id}"}}',
            },
        )
        current = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO documents (id, org_id, source_id, external_id, title, "
                "content_hash, acl_hash, body_original, body_masked, created_at) "
                "VALUES (:id, :org, :src, 'msg-1', 'Current version', 'h2', 'a', 'b', 'b', now())"
            ),
            {"id": current, "org": org_id, "src": source_id},
        )
        await db_session.execute(
            text(
                "INSERT INTO documents (id, org_id, source_id, external_id, title, "
                "content_hash, acl_hash, body_original, body_masked, superseded_by, created_at) "
                "VALUES (:id, :org, :src, 'msg-1', 'Old version', 'h1', 'a', 'b', 'b', "
                ":new, now())"
            ),
            {"id": uuid.uuid4(), "org": org_id, "src": source_id, "new": current},
        )

        try:
            body = (await client.get("/v1/integrations")).json()
            slack = next(item for item in body["items"] if item["id"] == "slack")
            assert slack["connection"]["document_count"] == 1, "current versions only"
        finally:
            # Migration 0010's downgrade deliberately refuses once anything has been
            # superseded, and this fixture's teardown downgrades to base — so the
            # version history seeded above must not outlive the test, even a failing
            # one, or every test after it fails at setup.
            await db_session.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
            )
            await db_session.execute(
                text("DELETE FROM documents WHERE source_id = :src"), {"src": source_id}
            )
            await db_session.commit()


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

    async def test_a_provider_blip_spends_the_state_anyway(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
    ) -> None:
        """The spend commits before the exchange, so a 503 cannot hand the state back.

        Spent on the request transaction, a provider failure would roll the spend back
        and leave the state replayable for its whole TTL — the exact shape the rate
        limiter's own-session commit exists to prevent.
        """

        class BlippingOAuth(FakeOAuth):
            """Down for exactly the first exchange, then healthy again."""

            def __init__(self) -> None:
                super().__init__()
                self.blips = 1

            async def exchange_code(
                self,
                provider: Provider,
                oauth_client: OAuthClient,
                *,
                code: str,
                redirect_uri: str,
                code_verifier: str | None = None,
            ) -> TokenGrant:
                if self.blips:
                    self.blips -= 1
                    raise ServiceUnavailable("The provider refused the token exchange.")
                return await super().exchange_code(
                    provider,
                    oauth_client,
                    code=code,
                    redirect_uri=redirect_uri,
                    code_verifier=code_verifier,
                )

        await register_owner(client, mailbox)
        _, state = await connect_slack(client)

        blipping = BlippingOAuth()
        client.app.dependency_overrides[get_oauth_transport] = lambda: blipping  # type: ignore[attr-defined]

        first = await complete(client, state)
        assert first.status_code == 503  # type: ignore[attr-defined]

        replay = await complete(client, state)
        assert replay.status_code == 404  # type: ignore[attr-defined]
        assert blipping.exchanges == [], "a spent state must never reach the provider again"

    async def test_a_denied_grant_redirects_back_with_a_reason(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        fake_oauth: FakeOAuth,
    ) -> None:
        """Clicking Deny lands a browser here with an error and no code.

        Raw 422 JSON would strand the person; instead the attempt is closed out — the
        state spent like any finished callback's — and the browser goes back to the
        Integrations page with a reason and nothing sensitive in the URL.
        """
        await register_owner(client, mailbox)
        _, state = await connect_slack(client)

        denied = await client.get(
            "/v1/connections/callback",
            params={"state": state, "error": "access_denied"},
            follow_redirects=False,
        )
        assert denied.status_code == 303
        assert denied.headers["location"] == "/me/integrations?connect_error=slack"
        assert fake_oauth.exchanges == [], "a denial must not reach the provider"

        body = (await client.get("/v1/integrations")).json()
        slack = next(item for item in body["items"] if item["id"] == "slack")
        assert slack["connection"]["status"] == "error", "the row says what happened"

        replay = await complete(client, state)
        assert replay.status_code == 404, "the denial spent the state"  # type: ignore[attr-defined]

    async def test_a_lost_insert_race_is_the_same_conflict_sentence(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        settings: Settings,
        db_session: AsyncSession,
    ) -> None:
        """Two tabs racing one Connect: the loser gets the pre-check's 409, not a 500.

        The interleaving cannot be produced over HTTP with one session, so the race is
        staged at the seam: a session that answers the pre-check honestly and then
        loses the INSERT to `uq_connections_live`, exactly as Postgres would report it.
        """

        class RacingSession:
            def __init__(self, real: AsyncSession) -> None:
                self._real = real

            def __getattr__(self, name: str) -> object:
                return getattr(self._real, name)

            async def execute(self, statement: Any, params: Any = None) -> Any:
                if "INSERT INTO connections" in str(statement):
                    raise IntegrityError(
                        "INSERT INTO connections",
                        params,
                        Exception(
                            'duplicate key value violates unique constraint "uq_connections_live"'
                        ),
                    )
                return await self._real.execute(statement, params)

        await register_owner(client, mailbox)
        org_id = uuid.UUID((await client.get("/v1/orgs/current")).json()["id"])
        user_id = uuid.UUID((await client.get("/v1/me")).json()["user_id"])
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
        )

        with pytest.raises(Conflict, match=r"Slack is already connected\."):
            await start_connection(
                RacingSession(db_session),  # type: ignore[arg-type]
                settings,
                org_id=org_id,
                user_id=user_id,
                provider_id="slack",
            )

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

    async def test_disconnect_survives_a_revocation_that_throws(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        inspector: AsyncSession,
    ) -> None:
        """Best-effort means ANY revocation failure, not only the ones httpx names.

        The local deletion is the authority; a transport that throws something
        unexpected must not leave a person unable to disconnect — or worse, leave the
        credential row behind because the request rolled back after deleting it.
        """

        class ExplodingRevokeOAuth(FakeOAuth):
            async def revoke_upstream(
                self, provider: Provider, oauth_client: OAuthClient, *, access_token: str
            ) -> None:
                raise RuntimeError("the revocation endpoint answered with garbage")

        await register_owner(client, mailbox)
        connection_id, state = await connect_slack(client)
        await complete(client, state)

        exploding = ExplodingRevokeOAuth()
        client.app.dependency_overrides[get_oauth_transport] = lambda: exploding  # type: ignore[attr-defined]

        response = await client.delete(f"/v1/me/connections/{connection_id}", headers=csrf(client))
        assert response.status_code == 204

        remaining = (
            await inspector.execute(text("SELECT count(*) FROM connection_credentials"))
        ).scalar_one()
        assert remaining == 0, "the local deletion is the authority and must still happen"

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


# --------------------------------------------------------------------------------------
# Sync now — the durable row and the doorbell
# --------------------------------------------------------------------------------------


class TestSyncNow:
    @pytest.fixture
    def rung(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        """Record doorbell rings instead of publishing to Redis inside a unit test."""
        calls: list[object] = []

        async def _record(org_id: object) -> bool:
            calls.append(org_id)
            return True

        monkeypatch.setattr("jutsu_api.routers.connections.ring_doorbell", _record)
        return calls

    async def test_sync_returns_the_job_that_will_actually_run(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        rung: list[object],
        inspector: AsyncSession,
    ) -> None:
        await register_owner(client, mailbox)
        connection_id, state = await connect_slack(client)
        await complete(client, state)

        first = await client.post(f"/v1/me/connections/{connection_id}/sync", headers=csrf(client))
        assert first.status_code == 202, first.text
        job_id = first.json()["job_id"]

        row = (
            await inspector.execute(
                text("SELECT id::text, state FROM jobs WHERE kind = 'connector.sync'")
            )
        ).one()
        assert row.id == job_id, "the reported id names the row that will run"
        assert row.state == "pending"

        # A second click while it is queued reports the SAME job, not a phantom id.
        second = await client.post(f"/v1/me/connections/{connection_id}/sync", headers=csrf(client))
        assert second.json()["job_id"] == job_id
        assert len(rung) == 2, "every request rings the doorbell"

    async def test_a_finished_sync_is_reopened_not_shadowed_forever(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        rung: list[object],
        inspector: AsyncSession,
    ) -> None:
        """A failed sync must not hold the idempotency key against every future click.

        The walk refuses to reopen failures because an automatic loop retries them for
        ever; here each attempt is one human click, so reopening is the honest choice —
        the alternative is a Sync button that silently does nothing for the rest of time.
        """
        await register_owner(client, mailbox)
        connection_id, state = await connect_slack(client)
        await complete(client, state)

        first = await client.post(f"/v1/me/connections/{connection_id}/sync", headers=csrf(client))
        job_id = first.json()["job_id"]
        await inspector.execute(
            text(
                "UPDATE jobs SET state = 'failed', attempts = 3, "
                "failure_kind = 'source_unavailable' WHERE id = :id"
            ),
            {"id": job_id},
        )
        await inspector.commit()

        again = await client.post(f"/v1/me/connections/{connection_id}/sync", headers=csrf(client))
        assert again.status_code == 202
        assert again.json()["job_id"] == job_id

        row = (
            await inspector.execute(
                text("SELECT state, attempts, failure_kind FROM jobs WHERE id = :id"),
                {"id": job_id},
            )
        ).one()
        assert row.state == "pending"
        assert row.attempts == 0
        assert row.failure_kind is None


# --------------------------------------------------------------------------------------
# OAuth hardening — PKCE, state expiry, upstream revocation, read-only registry
# --------------------------------------------------------------------------------------


class TestOAuthHardening:
    async def test_pkce_verifier_matches_the_challenge_the_browser_carried(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        fake_oauth: FakeOAuth,
    ) -> None:
        """The provider saw S256(verifier); the exchange must present that verifier."""
        import base64
        import hashlib
        from urllib.parse import parse_qs, urlparse

        await register_owner(client, mailbox)
        started = await client.post("/v1/me/connections/slack", headers=csrf(client))
        query = parse_qs(urlparse(started.json()["authorize_url"]).query)
        assert query["code_challenge_method"] == ["S256"]
        challenge = query["code_challenge"][0]
        state = query["state"][0]

        await complete(client, state)
        verifier = fake_oauth.verifiers[-1]
        assert verifier is not None
        derived = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        assert derived == challenge

    async def test_slack_asks_for_user_scopes_never_a_bot(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
    ) -> None:
        from urllib.parse import parse_qs, urlparse

        await register_owner(client, mailbox)
        started = await client.post("/v1/me/connections/slack", headers=csrf(client))
        query = parse_qs(urlparse(started.json()["authorize_url"]).query)
        assert "user_scope" in query, "Slack scopes go via user_scope (a user token)"
        assert "scope" not in query, "a `scope` here would provision a bot"
        assert "access_type" not in query, "a Google-only knob must not reach Slack"

    async def test_an_expired_state_is_a_404_like_any_forged_one(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        inspector: AsyncSession,
    ) -> None:
        await register_owner(client, mailbox)
        _connection_id, state = await connect_slack(client)

        await inspector.execute(
            text(
                "UPDATE connections SET oauth_state_expires_at = now() - interval '1 minute' "
                "WHERE oauth_state = :state"
            ),
            {"state": state},
        )
        await inspector.commit()

        response = await complete(client, state)
        assert response.status_code == 404  # type: ignore[attr-defined]

    async def test_disconnect_asks_the_provider_to_kill_the_grant(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        fake_oauth: FakeOAuth,
    ) -> None:
        await register_owner(client, mailbox)
        connection_id, state = await connect_slack(client)
        await complete(client, state)

        response = await client.delete(f"/v1/me/connections/{connection_id}", headers=csrf(client))
        assert response.status_code == 204
        assert fake_oauth.revoked == [ACCESS_TOKEN], (
            "the upstream grant is revoked with the token it protects, before the "
            "local ciphertext is deleted"
        )

    def test_no_provider_carries_a_write_scope(self) -> None:
        """§4.8 admits no write scope for any feature — pinned against the registry.

        `repo:status` was in the GitHub tuple once: GitHub documents it as read/WRITE
        on commit statuses. This list holds every scope string known to permit a write
        on its provider; a registry entry matching one is a defect whatever wanted it.
        """
        known_write_scopes = {
            "repo",
            "repo:status",
            "write:org",
            "admin:org",
            "gist",
            "chat:write",
            "channels:write",
            "files:write",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar",
            "Files.ReadWrite",
            "Sites.ReadWrite.All",
            "write:jira-work",
            "write:confluence-content",
        }
        for provider in PROVIDERS.values():
            overlap = set(provider.scopes) & known_write_scopes
            assert not overlap, f"{provider.id} carries write scope(s): {overlap}"


# --------------------------------------------------------------------------------------
# The real transport, deterministically — every branch against a scripted provider
# --------------------------------------------------------------------------------------


def _http_transport(handler: object) -> HttpOAuthTransport:
    """The real transport over `httpx.MockTransport` — its injection seam, exercised."""
    return HttpOAuthTransport(httpx.AsyncClient(transport=httpx.MockTransport(handler)))  # type: ignore[arg-type]


CLIENT = OAuthClient(client_id="cid", client_secret="cs")


class TestHttpOAuthTransport:
    async def test_slack_ok_false_is_a_refusal_not_a_token(self) -> None:
        """Slack answers HTTP 200 with ok=false; storing that body would put an error
        string where a token belongs."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "invalid_code"})

        with pytest.raises(ServiceUnavailable):
            await _http_transport(handler).exchange_code(
                PROVIDERS["slack"], CLIENT, code="c", redirect_uri="https://app/cb"
            )

    async def test_slack_grant_comes_from_authed_user_never_the_bot(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "access_token": "xoxb-bot-token",
                    "authed_user": {
                        "access_token": "xoxp-user-token",
                        "refresh_token": "xoxe-refresh",
                        "expires_in": 3600,
                    },
                },
            )

        grant = await _http_transport(handler).exchange_code(
            PROVIDERS["slack"], CLIENT, code="c", redirect_uri="https://app/cb"
        )
        assert grant.access_token == "xoxp-user-token", "the employee's token, not a bot's"
        assert grant.refresh_token == "xoxe-refresh"
        assert grant.expires_in == 3600

    async def test_a_failed_exchange_forwards_nothing_from_the_body(self) -> None:
        """The body can carry the code and client id back; classify, never forward."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="error=bad_verification_code&code=SENTINEL-code")

        with pytest.raises(ServiceUnavailable) as refusal:
            await _http_transport(handler).exchange_code(
                PROVIDERS["github"], CLIENT, code="c", redirect_uri="https://app/cb"
            )
        assert "SENTINEL" not in str(refusal.value)
        assert "bad_verification_code" not in str(refusal.value)

    @pytest.mark.parametrize(
        ("payload", "subject", "label"),
        [
            ({"sub": "google-sub", "account_id": "shadow", "email": "a@b"}, "google-sub", "a@b"),
            ({"account_id": "atlassian-acct", "name": "Ada"}, "atlassian-acct", "Ada"),
            ({"ok": True, "user_id": "U123", "user": "ada"}, "U123", "ada"),  # Slack auth.test
            ({"id": 4242, "login": "octo"}, "4242", "octo"),
        ],
    )
    async def test_identity_subject_fallback_chain(
        self, payload: dict[str, object], subject: str, label: str
    ) -> None:
        """sub → account_id → user_id → id, in that order, across the providers'
        identity shapes — including Slack's auth.test, which has none of OIDC's."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        identity = await _http_transport(handler).fetch_identity(PROVIDERS["github"], "tok")
        assert identity.subject == subject
        assert identity.label == label

    async def test_revoke_styles_send_what_each_provider_documents(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        transport = _http_transport(handler)

        await transport.revoke_upstream(PROVIDERS["google_drive"], CLIENT, access_token="tok-g")
        google = seen.pop()
        assert google.method == "POST"
        assert str(google.url) == "https://oauth2.googleapis.com/revoke"
        assert b"token=tok-g" in google.content

        await transport.revoke_upstream(PROVIDERS["slack"], CLIENT, access_token="tok-s")
        slack = seen.pop()
        assert slack.method == "POST"
        assert slack.headers["Authorization"] == "Bearer tok-s"

        await transport.revoke_upstream(PROVIDERS["github"], CLIENT, access_token="tok-h")
        github = seen.pop()
        assert github.method == "DELETE"
        assert str(github.url) == "https://api.github.com/applications/cid/grant"
        assert github.headers["Authorization"].startswith("Basic ")
        assert b"tok-h" in github.content

        # No declared endpoint (Microsoft relies on expiry): nothing is sent at all.
        await transport.revoke_upstream(PROVIDERS["onedrive"], CLIENT, access_token="tok-m")
        assert seen == []

    async def test_an_unreachable_revocation_endpoint_is_swallowed(self) -> None:
        """Disconnecting must not depend on a provider being up."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nothing listening")

        await _http_transport(handler).revoke_upstream(
            PROVIDERS["slack"], CLIENT, access_token="tok"
        )


# --------------------------------------------------------------------------------------
# ADR 0014 — the callback links the proven subject
# --------------------------------------------------------------------------------------


class TestVerifiedSubjectLink:
    async def test_completing_a_callback_links_the_proven_subject(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        inspector: AsyncSession,
    ) -> None:
        await register_owner(client, mailbox)
        _connection_id, state = await connect_slack(client)
        await complete(client, state)

        row = (
            await inspector.execute(
                text(
                    "SELECT source_system::text AS source_system, subject, linked_by, "
                    "is_active FROM source_identities WHERE source_system = 'slack'"
                )
            )
        ).one()
        assert row.subject == "U0AB12CD", "the subject the provider proved, verbatim"
        assert row.linked_by == "oauth_connection"
        assert row.is_active is True

    async def test_a_subject_already_held_by_a_colleague_links_nothing(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        inspector: AsyncSession,
    ) -> None:
        """Fail-closed: one subject is one person per tenant; nobody's access moves."""
        await register_owner(client, mailbox)
        owner_id = (
            await inspector.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": OWNER_EMAIL}
            )
        ).scalar_one()
        org_id = (
            await inspector.execute(
                text("SELECT org_id FROM users WHERE id = :id"), {"id": owner_id}
            )
        ).scalar_one()

        await invite_and_accept(client, mailbox, email="rival@example.com")
        # The invited member session is now active; give the OWNER's slack subject to
        # the member first, then have the member connect slack and prove that subject.
        # (FakeOAuth always answers U0AB12CD.)
        await inspector.execute(
            text(
                "INSERT INTO source_identities (org_id, user_id, source_system, subject, "
                "linked_by) VALUES (:org, :user, 'slack', 'U0AB12CD', 'admin')"
            ),
            {"org": str(org_id), "user": owner_id},
        )
        await inspector.commit()

        _connection_id, state = await connect_slack(client)
        response = await complete(client, state)
        assert response.status_code == 303  # type: ignore[attr-defined]

        rows = (
            await inspector.execute(
                text(
                    "SELECT user_id FROM source_identities "
                    "WHERE source_system = 'slack' AND subject = 'U0AB12CD'"
                )
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].user_id == owner_id, "the existing holder keeps the subject"

    async def test_disconnecting_does_not_revoke_the_identity(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        oauth_env: None,
        inspector: AsyncSession,
    ) -> None:
        """Who somebody is does not change when a pipe closes (ADR 0014)."""
        await register_owner(client, mailbox)
        connection_id, state = await connect_slack(client)
        await complete(client, state)
        await client.delete(f"/v1/me/connections/{connection_id}", headers=csrf(client))

        row = (
            await inspector.execute(
                text(
                    "SELECT is_active, revoked_at FROM source_identities "
                    "WHERE source_system = 'slack' AND subject = 'U0AB12CD'"
                )
            )
        ).one()
        assert row.is_active is True
        assert row.revoked_at is None

    def test_every_provider_declares_a_valid_acl_namespace(self) -> None:
        from jutsu_core.models import SourceSystem

        for provider in PROVIDERS.values():
            assert provider.acl_namespace, f"{provider.id} declares no ACL namespace"
            SourceSystem(provider.acl_namespace)  # raises on an unknown value
