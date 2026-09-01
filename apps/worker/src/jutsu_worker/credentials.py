"""Provider credentials for sync fetchers: decrypt, refresh, and fail closed.

The API stores tokens; this module is the only place that reads them back. Three
outcomes, each with a distinct shape because each demands a different operator action:

* a usable token — decrypted, refreshed first if stale, returned to the fetcher and
  held only in memory for the life of one sync;
* `ReauthRequired` — the provider rejected the refresh (revoked grant, expired
  refresh token). No retry fixes it; the person must reconnect, so the sync path
  flips the connection to `reauth_required` where the UI offers exactly that;
* `CredentialsUnavailable` — the *deployment* cannot read what it stored (no Fernet
  key, no client registration). Also permanent, but the fix is an operator's, and the
  message says so without naming any secret.

Refreshed tokens are re-encrypted and stored in the same transaction as the sync work
that needed them, so a crash rolls back both together. Atlassian rotates refresh
tokens on every use — the returned one always replaces the stored one when present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from jutsu_core.providers import PROVIDERS, oauth_client_for
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CredentialsUnavailable",
    "ReauthRequired",
    "TransientRefreshError",
    "access_token_for",
    "mark_reauth_required",
]

#: A token this close to expiry is refreshed before use: a sync that starts with ten
#: seconds of validity fails half-way with a worse error than a refresh would.
_EXPIRY_MARGIN = timedelta(seconds=120)


class ReauthRequired(RuntimeError):
    """The provider no longer honours this grant. Only its owner reconnecting fixes it."""


class CredentialsUnavailable(RuntimeError):
    """The deployment cannot use the stored credential (key or client registration
    missing). An operator's problem, never retried into working."""


class TransientRefreshError(RuntimeError):
    """The refresh endpoint misbehaved in a way retrying may fix (5xx, network)."""


def _fernet() -> Fernet:
    key = os.environ.get("JUTSU_CONNECTION_KEY", "").strip()
    if not key:
        raise CredentialsUnavailable(
            "JUTSU_CONNECTION_KEY is not set; stored provider tokens cannot be read."
        )
    return Fernet(key.encode("ascii"))


@dataclass(frozen=True, slots=True)
class _StoredCredential:
    provider_id: str
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None


async def _load(session: AsyncSession, connection_id: UUID) -> _StoredCredential:
    row = (
        await session.execute(
            text(
                "SELECT c.provider, cc.access_token_enc, cc.refresh_token_enc, "
                "cc.token_expires_at FROM connections c "
                "JOIN connection_credentials cc ON cc.connection_id = c.id "
                "WHERE c.id = :id"
            ),
            {"id": connection_id},
        )
    ).first()
    if row is None:
        raise ReauthRequired("No stored credential for this connection.")
    fernet = _fernet()
    try:
        access = fernet.decrypt(bytes(row.access_token_enc)).decode("utf-8")
        refresh = (
            fernet.decrypt(bytes(row.refresh_token_enc)).decode("utf-8")
            if row.refresh_token_enc is not None
            else None
        )
    except InvalidToken as error:
        # A rotated Fernet key cannot decrypt old ciphertext. The stored credential is
        # unusable, and pretending otherwise would burn a provider call to learn less.
        raise CredentialsUnavailable(
            "Stored credential ciphertext cannot be decrypted with the current key."
        ) from error
    return _StoredCredential(
        provider_id=row.provider,
        access_token=access,
        refresh_token=refresh,
        expires_at=row.token_expires_at,
    )


async def _store(
    session: AsyncSession,
    connection_id: UUID,
    *,
    access_token: str,
    refresh_token: str | None,
    expires_in: int | None,
) -> None:
    fernet = _fernet()
    await session.execute(
        text(
            "UPDATE connection_credentials SET access_token_enc = :access, "
            "refresh_token_enc = COALESCE(:refresh, refresh_token_enc), "
            "token_expires_at = :expires_at, updated_at = now() "
            "WHERE connection_id = :id"
        ),
        {
            "id": connection_id,
            "access": fernet.encrypt(access_token.encode("utf-8")),
            "refresh": (fernet.encrypt(refresh_token.encode("utf-8")) if refresh_token else None),
            "expires_at": (
                datetime.now(tz=UTC) + timedelta(seconds=expires_in)
                if expires_in is not None
                else None
            ),
        },
    )


async def access_token_for(
    session: AsyncSession,
    *,
    connection_id: UUID,
    http: httpx.AsyncClient | None = None,
) -> str:
    """A currently-valid access token for this connection, refreshing if stale.

    The refreshed ciphertext is written on the caller's session — the same transaction
    as the sync work — so a crash rolls both back together and the old (still-valid at
    the provider) refresh token is not lost.
    """
    stored = await _load(session, connection_id)

    fresh_enough = stored.expires_at is None or stored.expires_at > (
        datetime.now(tz=UTC) + _EXPIRY_MARGIN
    )
    if fresh_enough:
        return stored.access_token

    if stored.refresh_token is None:
        raise ReauthRequired("The access token expired and no refresh token was granted.")

    provider = PROVIDERS.get(stored.provider_id)
    client = oauth_client_for(stored.provider_id)
    if provider is None or client is None:
        raise CredentialsUnavailable("The provider's client registration is no longer configured.")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": stored.refresh_token,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
    }
    owns_client = http is None
    http = http or httpx.AsyncClient(timeout=20.0)
    try:
        response = await http.post(
            provider.token_url, data=data, headers={"Accept": "application/json"}
        )
    except httpx.HTTPError as error:
        raise TransientRefreshError("The provider was unreachable for a token refresh.") from error
    finally:
        if owns_client:
            await http.aclose()

    if response.status_code in (400, 401):
        # invalid_grant and friends: the grant itself is dead. Only reconnecting helps.
        raise ReauthRequired("The provider rejected the refresh token.")
    if response.status_code != 200:
        raise TransientRefreshError("The provider failed the token refresh.")

    payload = response.json()
    if payload.get("ok") is False:
        raise ReauthRequired("The provider rejected the refresh token.")
    if provider.token_style == "slack_user" and isinstance(  # noqa: S105 - style tag
        payload.get("authed_user"), dict
    ):
        payload = payload["authed_user"]

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise TransientRefreshError("The provider's refresh response was not usable.")
    rotated = payload.get("refresh_token")
    expires = payload.get("expires_in")

    await _store(
        session,
        connection_id,
        access_token=access_token,
        refresh_token=rotated if isinstance(rotated, str) else None,
        expires_in=int(expires) if isinstance(expires, int) else None,
    )
    return access_token


async def mark_reauth_required(session: AsyncSession, *, connection_id: UUID) -> None:
    """Flip the connection so its owner sees Reconnect instead of a dead Sync button.

    Runs in its own transaction after the job failure is recorded, exactly like
    `mark_sync_unavailable` — the job ledger is the authority, this is the owner-facing
    annotation.
    """
    await session.execute(
        text(
            "UPDATE connections SET status = 'reauth_required', "
            "last_error_kind = 'reauth_required', updated_at = now() WHERE id = :id"
        ),
        {"id": connection_id},
    )
