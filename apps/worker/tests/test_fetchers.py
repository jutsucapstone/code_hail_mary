"""The worker-side connector factory and its token source.

Two contracts matter here and nowhere else. `ConnectionTokenSource` commits a refresh
in its OWN transaction — Atlassian burns the old refresh token the moment it answers,
so a rotation tied to the fate of a walk manufactures a permanent reauth loop out of a
transient failure. And `build_provider_connector` refuses a misconfigured source as
`UnsupportedSource`, permanent, because re-reading the same row gives the same answer.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from jutsu_core.models import SourceSystem
from jutsu_db.engine import dispose_engine, org_session
from jutsu_worker.fetchers import ConnectionTokenSource, build_provider_connector
from jutsu_worker.registry import UnsupportedSource, close_connector
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
    """Migrated schema, app-role engine, disposed on BOTH sides — the process-cached
    engine trap, same as test_ingest_pipeline. Inline rather than in a conftest because
    mypy refuses a second module named conftest under apps/."""
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
    provider_subject: str | None = None,
    access_token: str = "live-access",  # noqa: S107 - test fixture value
    refresh_token: str | None = "live-refresh",  # noqa: S107 - test fixture value
    expires_at: datetime | None = None,
) -> uuid.UUID:
    user_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    async with org_session(org_id) as session:
        await session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, 'fetcher-test')"), {"id": org_id}
        )
        await session.execute(
            text(
                "INSERT INTO users (id, org_id, email, status) "
                "VALUES (:id, :org, 'owner@fetcher.test', 'active')"
            ),
            {"id": user_id, "org": org_id},
        )
        await session.execute(
            text(
                "INSERT INTO connections (id, org_id, user_id, provider, status, "
                "provider_subject) VALUES (:id, :org, :user, :provider, 'connected', :subject)"
            ),
            {
                "id": connection_id,
                "org": org_id,
                "user": user_id,
                "provider": provider,
                "subject": provider_subject,
            },
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


def _install_refresh_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    """Route the refresh POST that `access_token_for` makes with its self-built client.

    `ConnectionTokenSource` passes no client, so the fake provider cannot be injected
    through the parameter — it goes in through the credentials module's `httpx`,
    shimmed so only that module sees it.
    """
    import jutsu_worker.credentials as credentials

    real_client = httpx.AsyncClient

    def build_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=transport)

    monkeypatch.setattr(
        credentials,
        "httpx",
        SimpleNamespace(AsyncClient=build_client, HTTPError=httpx.HTTPError),
    )


class TestConnectionTokenSource:
    async def test_a_refresh_commits_independently_of_the_walks_transaction(
        self, fernet_key: Fernet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rotated ciphertext must survive the walk dying: the provider burned the
        old refresh token the moment it answered, so rolling the rotation back with a
        failed sync would leave a token the provider never honours again."""
        monkeypatch.setenv("JUTSU_OAUTH_JIRA_CLIENT_ID", "jira-client")
        monkeypatch.setenv("JUTSU_OAUTH_JIRA_CLIENT_SECRET", "jira-secret")
        org_id = uuid.uuid4()
        connection_id = await seed_connection(
            org_id, fernet_key, expires_at=datetime.now(tz=UTC) - timedelta(minutes=1)
        )

        def refresh(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "renewed-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                },
            )

        _install_refresh_transport(monkeypatch, httpx.MockTransport(refresh))
        source = ConnectionTokenSource(org_id, connection_id)

        class WalkDied(RuntimeError):
            pass

        with pytest.raises(WalkDied):
            async with org_session(org_id) as walk:
                token = await source.access_token()
                assert token == "renewed-access"
                # A write the walk WILL roll back, to prove the two fates are separate.
                await walk.execute(
                    text("UPDATE connections SET last_error_kind = 'sync_unavailable'")
                )
                raise WalkDied

        async with org_session(org_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT c.last_error_kind, cc.refresh_token_enc "
                        "FROM connections c JOIN connection_credentials cc "
                        "ON cc.connection_id = c.id WHERE c.id = :id"
                    ),
                    {"id": connection_id},
                )
            ).one()
        assert row.last_error_kind is None, "the walk's own write rolled back"
        assert fernet_key.decrypt(bytes(row.refresh_token_enc)).decode() == "rotated-refresh", (
            "the rotation did not"
        )

    async def test_a_cached_token_spares_the_database_for_a_minute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One credential read per minute, not per request: a walk makes hundreds of
        provider calls, and each one re-fetching, decrypting and possibly refreshing
        would turn the token store into the walk's hot path."""
        import jutsu_worker.fetchers as fetchers

        calls = 0

        async def counting(session: AsyncSession, *, connection_id: uuid.UUID) -> str:
            nonlocal calls
            calls += 1
            return "cached-token"

        monkeypatch.setattr(fetchers, "access_token_for", counting)
        source = ConnectionTokenSource(uuid.uuid4(), uuid.uuid4())

        assert await source.access_token() == "cached-token"
        assert await source.access_token() == "cached-token"
        assert calls == 1, "the second call within the window is served from memory"


class TestBuildProviderConnector:
    async def test_a_config_naming_no_connection_is_refused(self) -> None:
        with pytest.raises(UnsupportedSource):
            await build_provider_connector(
                SourceSystem.GITHUB, {"provider": "github"}, org_id=uuid.uuid4()
            )

    async def test_an_unknown_provider_is_refused(self) -> None:
        config = {"connection_id": str(uuid.uuid4()), "provider": "gitlab"}
        with pytest.raises(UnsupportedSource):
            await build_provider_connector(SourceSystem.GITHUB, config, org_id=uuid.uuid4())

    async def test_a_vanished_connection_is_refused(self) -> None:
        org_id = uuid.uuid4()
        async with org_session(org_id) as session:
            await session.execute(
                text("INSERT INTO orgs (id, name) VALUES (:id, 'fetcher-test')"), {"id": org_id}
            )
        config = {"connection_id": str(uuid.uuid4()), "provider": "github"}
        with pytest.raises(UnsupportedSource):
            await build_provider_connector(SourceSystem.GITHUB, config, org_id=org_id)

    async def test_a_connection_without_a_proven_subject_is_refused(
        self, fernet_key: Fernet
    ) -> None:
        """Rows connected before subjects were stored: no subject means no principal to
        mint grants for, and a guessed one would be a grant (ADR 0014)."""
        org_id = uuid.uuid4()
        connection_id = await seed_connection(
            org_id, fernet_key, provider="github", provider_subject=None
        )
        config = {"connection_id": str(connection_id), "provider": "github"}
        with pytest.raises(UnsupportedSource):
            await build_provider_connector(SourceSystem.GITHUB, config, org_id=org_id)

    async def test_a_built_connector_carries_the_rows_subject(self, fernet_key: Fernet) -> None:
        org_id = uuid.uuid4()
        connection_id = await seed_connection(
            org_id, fernet_key, provider="github", provider_subject="583231"
        )
        config = {"connection_id": str(connection_id), "provider": "github"}

        connector = await build_provider_connector(SourceSystem.GITHUB, config, org_id=org_id)
        try:
            assert connector.system is SourceSystem.GITHUB
            # Through the protocol, not the private context: the grant every fetched
            # document gets is the proof the subject came from the connection row.
            grants = await connector.acls("readme:octocat/hello-world")
            assert [entry.principal_id for entry in grants] == ["github:583231"]
        finally:
            await close_connector(connector)
