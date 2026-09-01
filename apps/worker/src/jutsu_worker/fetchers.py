"""Where provider connectors meet credentials: the worker-side factory.

`jutsu_connectors.providers` implements the fetching and knows nothing about the
database; `jutsu_worker.credentials` implements decrypt-and-refresh and knows nothing
about providers' APIs. This module is the only place the two meet, and it is the only
place a provider connector is ever constructed in production.

The token source commits refreshed ciphertext in its OWN transaction, immediately.
That is deliberate and load-bearing: Atlassian rotates refresh tokens on every use, so
the moment the provider answers a refresh, the old token is burned at their end.
Rolling the new one back with a failed sync would leave the row holding a token the
provider will never honour again — a permanent reauth loop manufactured out of a
transient failure.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
from jutsu_connectors.providers import CONNECTOR_CLASSES
from jutsu_connectors.providers.base import ProviderContext
from jutsu_core.models import Connector, SourceSystem
from jutsu_db.engine import org_session
from sqlalchemy import text

from jutsu_worker.credentials import access_token_for
from jutsu_worker.registry import UnsupportedSource

__all__ = ["ConnectionTokenSource", "build_provider_connector"]

#: How long a fetched token is reused in memory before asking the credential layer
#: again. Well inside the 120-second refresh margin `access_token_for` keeps, so a
#: cached token is never one the layer would already have refreshed.
_TOKEN_CACHE_SECONDS = 60.0


class ConnectionTokenSource:
    """A `TokenSource` bound to one connection, refreshing through the credential layer.

    Each fetch opens its own org-scoped session and commits it, so a refresh performed
    mid-walk survives whatever happens to the walk (see the module docstring for why
    rotation makes that mandatory, not merely convenient).
    """

    __slots__ = ("_cached", "_connection_id", "_fetched_at", "_org_id")

    def __init__(self, org_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        self._org_id = org_id
        self._connection_id = connection_id
        self._cached: str | None = None
        self._fetched_at = 0.0

    async def access_token(self) -> str:
        now = time.monotonic()
        if self._cached is not None and now - self._fetched_at < _TOKEN_CACHE_SECONDS:
            return self._cached
        async with org_session(self._org_id) as session:
            token = await access_token_for(session, connection_id=self._connection_id)
        self._cached = token
        self._fetched_at = now
        return token


async def build_provider_connector(
    system: SourceSystem, config: dict[str, Any], *, org_id: uuid.UUID
) -> Connector:
    """The connector for a provider-backed source row.

    The source's `config_json` names the connection; the connection row supplies the
    proven subject (ADR 0014) and, through the credential layer, the token. Every
    refusal here is `UnsupportedSource` — permanent, because re-reading the same
    misconfigured row gives the same answer.
    """
    raw_connection_id = config.get("connection_id")
    provider_id = config.get("provider")
    if not isinstance(raw_connection_id, str) or not isinstance(provider_id, str):
        raise UnsupportedSource("provider source config names no connection")
    connector_class = CONNECTOR_CLASSES.get(provider_id)
    if connector_class is None:
        raise UnsupportedSource(f"no sync fetcher for provider '{provider_id}' yet")
    connection_id = uuid.UUID(raw_connection_id)

    async with org_session(org_id) as session:
        row = (
            await session.execute(
                text("SELECT provider_subject, status FROM connections WHERE id = :id"),
                {"id": connection_id},
            )
        ).first()
    if row is None:
        raise UnsupportedSource("the connection behind this source no longer exists")
    if not row.provider_subject:
        # Rows connected before subjects were stored: reconnecting proves one.
        raise UnsupportedSource("the connection carries no proven subject; reconnect it")

    context = ProviderContext(namespace=SourceSystem(system), subject=str(row.provider_subject))
    token = ConnectionTokenSource(org_id, connection_id)
    # follow_redirects for the providers that answer content requests with a 302 to a
    # pre-authorised download URL (Microsoft Graph); harmless everywhere else.
    client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    connector: Connector = connector_class(context, token, client)
    return connector
