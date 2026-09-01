"""Token lifecycle for sync fetchers: decrypt, refresh, rotate, fail closed.

Every test runs against real Postgres and real Fernet ciphertext — the one thing faked
is the provider's token endpoint (httpx.MockTransport), because a unit test must not
bill Atlassian to prove a refresh body parses.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from jutsu_db.engine import dispose_engine, org_session
from jutsu_worker.credentials import (
    CredentialsUnavailable,
    ReauthRequired,
    access_token_for,
    mark_reauth_required,
)
from sqlalchemy import text

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"

pytestmark = pytest.mark.usefixtures("worker_database")


def _alembic_config(url: str) -> Config:
    root = Path(__file__).resolve().parents[3] / "packages" / "db"
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src" / "jutsu_db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
async def worker_database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    if os.environ.get("JUTSU_DB_REACHABLE") != "1":
        pytest.skip(f"nothing listening at {TEST_DB_ENV}")
    app_url = os.environ[TEST_DB_ENV]
    migration_url = os.environ.get(MIGRATION_DB_ENV, app_url)

    cfg = _alembic_config(migration_url)
    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")

    monkeypatch.setenv("DATABASE_URL", app_url)
    await dispose_engine()
    yield
    await dispose_engine()
    await asyncio.to_thread(command.downgrade, cfg, "base")


@pytest.fixture
def fernet_key(monkeypatch: pytest.MonkeyPatch) -> Fernet:
    key = Fernet.generate_key()
    monkeypatch.setenv("JUTSU_CONNECTION_KEY", key.decode("ascii"))
    return Fernet(key)


async def seed_connection(
    org_id: uuid.UUID,
    fernet: Fernet,
    *,
    provider: str = "jira",
    access_token: str = "live-access",  # noqa: S107 - test fixture value
    refresh_token: str | None = "live-refresh",  # noqa: S107 - test fixture value
    expires_at: datetime | None = None,
) -> uuid.UUID:
    user_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    async with org_session(org_id) as session:
        await session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, 'cred-test')"), {"id": org_id}
        )
        await session.execute(
            text(
                "INSERT INTO users (id, org_id, email, status) "
                "VALUES (:id, :org, 'owner@cred.test', 'active')"
            ),
            {"id": user_id, "org": org_id},
        )
        await session.execute(
            text(
                "INSERT INTO connections (id, org_id, user_id, provider, status) "
                "VALUES (:id, :org, :user, :provider, 'connected')"
            ),
            {"id": connection_id, "org": org_id, "user": user_id, "provider": provider},
        )
        await session.execute(
            text(
                "INSERT INTO connection_credentials (connection_id, org_id, "
                "access_token_enc, refresh_token_enc, token_expires_at) "
                "VALUES (:id, :org, :access, :refresh, :expires)"
            ),
            {
                "id": connection_id,
                "org": str(org_id),
                "access": fernet.encrypt(access_token.encode()),
                "refresh": fernet.encrypt(refresh_token.encode()) if refresh_token else None,
                "expires": expires_at,
            },
        )
    return connection_id


def _refusing_http() -> httpx.AsyncClient:
    def deny(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no provider call was expected")

    return httpx.AsyncClient(transport=httpx.MockTransport(deny))


class TestAccessToken:
    async def test_a_fresh_token_is_returned_without_touching_the_provider(
        self, fernet_key: Fernet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        org_id = uuid.uuid4()
        connection_id = await seed_connection(
            org_id, fernet_key, expires_at=datetime.now(tz=UTC) + timedelta(hours=1)
        )
        async with org_session(org_id) as session, _refusing_http() as http:
            token = await access_token_for(session, connection_id=connection_id, http=http)
        assert token == "live-access"

    async def test_a_stale_token_refreshes_and_rotates_the_stored_ciphertext(
        self, fernet_key: Fernet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JUTSU_OAUTH_JIRA_CLIENT_ID", "jira-client")
        monkeypatch.setenv("JUTSU_OAUTH_JIRA_CLIENT_SECRET", "jira-secret")
        org_id = uuid.uuid4()
        connection_id = await seed_connection(
            org_id, fernet_key, expires_at=datetime.now(tz=UTC) - timedelta(minutes=1)
        )

        seen: list[dict[str, str]] = []

        def refresh(request: httpx.Request) -> httpx.Response:
            form = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
            seen.append(form)
            return httpx.Response(
                200,
                json={
                    "access_token": "renewed-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                },
            )

        async with (
            org_session(org_id) as session,
            httpx.AsyncClient(transport=httpx.MockTransport(refresh)) as http,
        ):
            token = await access_token_for(session, connection_id=connection_id, http=http)
            assert token == "renewed-access"

        assert seen[0]["grant_type"] == "refresh_token"
        async with org_session(org_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT access_token_enc, refresh_token_enc, token_expires_at "
                        "FROM connection_credentials WHERE connection_id = :id"
                    ),
                    {"id": connection_id},
                )
            ).one()
        assert fernet_key.decrypt(bytes(row.access_token_enc)).decode() == "renewed-access"
        # Atlassian rotates refresh tokens; the rotated one must replace the stored one
        # or the NEXT refresh presents a token the provider already burned.
        assert fernet_key.decrypt(bytes(row.refresh_token_enc)).decode() == "rotated-refresh"
        assert row.token_expires_at is not None

    async def test_a_zoom_refresh_authenticates_with_basic_never_the_body(
        self, fernet_key: Fernet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`token_auth="basic"` (Zoom): the client goes in an HTTP Basic header and
        stays out of the form body — the twin of the API exchange's pin, because both
        callers of a token URL must honour the same registry declaration."""
        monkeypatch.setenv("JUTSU_OAUTH_ZOOM_CLIENT_ID", "zoom-client")
        monkeypatch.setenv("JUTSU_OAUTH_ZOOM_CLIENT_SECRET", "zoom-secret")
        org_id = uuid.uuid4()
        connection_id = await seed_connection(
            org_id,
            fernet_key,
            provider="zoom",
            expires_at=datetime.now(tz=UTC) - timedelta(minutes=1),
        )

        captured: dict[str, str] = {}

        def refresh(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("Authorization", "")
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "zoomed", "expires_in": 3600})

        async with (
            org_session(org_id) as session,
            httpx.AsyncClient(transport=httpx.MockTransport(refresh)) as http,
        ):
            token = await access_token_for(session, connection_id=connection_id, http=http)
        assert token == "zoomed"
        import base64

        expected = base64.b64encode(b"zoom-client:zoom-secret").decode("ascii")
        assert captured["authorization"] == f"Basic {expected}"
        assert "client_secret" not in captured["body"]
        assert "client_id" not in captured["body"]

    async def test_a_rejected_refresh_is_reauth_and_the_owner_sees_reconnect(
        self, fernet_key: Fernet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JUTSU_OAUTH_JIRA_CLIENT_ID", "jira-client")
        monkeypatch.setenv("JUTSU_OAUTH_JIRA_CLIENT_SECRET", "jira-secret")
        org_id = uuid.uuid4()
        connection_id = await seed_connection(
            org_id, fernet_key, expires_at=datetime.now(tz=UTC) - timedelta(minutes=1)
        )

        def reject(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        async with (
            org_session(org_id) as session,
            httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http,
        ):
            with pytest.raises(ReauthRequired):
                await access_token_for(session, connection_id=connection_id, http=http)

        async with org_session(org_id) as session:
            await mark_reauth_required(session, connection_id=connection_id)
        async with org_session(org_id) as session:
            row = (
                await session.execute(
                    text("SELECT status, last_error_kind FROM connections WHERE id = :id"),
                    {"id": connection_id},
                )
            ).one()
        assert row.status == "reauth_required"
        assert row.last_error_kind == "reauth_required"

    async def test_a_missing_key_is_the_deployments_fault_not_the_grants(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fernet = Fernet(Fernet.generate_key())
        org_id = uuid.uuid4()
        connection_id = await seed_connection(org_id, fernet)
        monkeypatch.delenv("JUTSU_CONNECTION_KEY", raising=False)

        async with org_session(org_id) as session:
            with pytest.raises(CredentialsUnavailable):
                await access_token_for(session, connection_id=connection_id)

    async def test_no_secret_appears_in_the_exception_text(
        self, fernet_key: Fernet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§4.9 for errors: a refresh failure names nothing it protects."""
        monkeypatch.setenv("JUTSU_OAUTH_JIRA_CLIENT_ID", "jira-client")
        monkeypatch.setenv("JUTSU_OAUTH_JIRA_CLIENT_SECRET", "jira-secret")
        org_id = uuid.uuid4()
        connection_id = await seed_connection(
            org_id, fernet_key, expires_at=datetime.now(tz=UTC) - timedelta(minutes=1)
        )

        def reject(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        async with (
            org_session(org_id) as session,
            httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http,
        ):
            with pytest.raises(ReauthRequired) as excinfo:
                await access_token_for(session, connection_id=connection_id, http=http)
        rendered = json.dumps(str(excinfo.value))
        for secret in ("live-access", "live-refresh", "jira-secret"):
            assert secret not in rendered
