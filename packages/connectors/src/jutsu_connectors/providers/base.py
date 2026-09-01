"""Shared plumbing for live provider connectors.

Three decisions live here so ten connectors cannot make them ten ways:

* **Failure taxonomy.** A provider call fails as `ProviderApiError(transient=...)` —
  429/5xx/network are transient (the job's retry ladder handles them, honouring
  Retry-After via the scheduler's backoff), any other 4xx is permanent (rejected
  identically every time), and 401/403 is `ProviderAuthError`, its own type because
  the operator action is different: the *grant* died, not the request.
* **ACL floor and ceiling.** Until a fetcher can map provider-side sharing onto
  provider-native *subjects* (ADR 0014: emails are not subjects), every fetched
  document is granted to exactly the connecting user. `owner_acl` is the only way a
  provider module mints grants, so widening is a visible diff in one place, never a
  drive-by.
* **Cursor discipline.** The walk hands connectors an ISO-8601 instant (or None for
  the first sync). `parse_cursor` is the one parser; a connector that cannot filter
  server-side filters client-side, and either way an unchanged document re-listed
  costs one `unchanged` outcome, never a duplicate — content hashing downstream is
  the authority.

No module here opens a database session or reads an environment variable. Tokens
arrive through `TokenSource` (the worker injects decryption and refresh), HTTP through
an injectable client (tests use `httpx.MockTransport`; nothing in this package's test
suite may reach a network).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from jutsu_core.models import AclEntry, SourceSystem

__all__ = [
    "ProviderApiError",
    "ProviderAuthError",
    "ProviderContext",
    "ProviderHttp",
    "TokenSource",
    "owner_acl",
    "parse_cursor",
]


class TokenSource(Protocol):
    """A currently-valid access token, however the caller keeps it valid."""

    async def access_token(self) -> str: ...


class ProviderApiError(RuntimeError):
    """The provider's API refused or failed a call.

    `transient` decides the job's fate: True retries under the ladder, False fails
    permanently. `retry_after` is advisory (seconds) when the provider sent one.
    The message never carries a response body — provider errors quote the request,
    and the request carries the token.
    """

    def __init__(self, message: str, *, transient: bool, retry_after: float | None = None):
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after


class ProviderAuthError(ProviderApiError):
    """The provider no longer honours the token (401/403). Permanent by construction:
    the fix is the owner reconnecting, not another attempt."""

    def __init__(self, message: str):
        super().__init__(message, transient=False)


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """What the worker proves before a connector runs: whose visibility this is.

    `subject` is the provider-native subject the OAuth callback verified (never an
    email), `namespace` the SourceSystem its principals are minted in.
    """

    namespace: SourceSystem
    subject: str


def owner_acl(context: ProviderContext) -> list[AclEntry]:
    """The connecting user's own grant — the floor and, for now, the ceiling.

    Widening beyond the owner requires provider-side sharing data expressed as
    *subjects*; a provider that only reports emails cannot prove who a document is
    shared with in ACL terms, and a guess would be a grant (ADR 0014).
    """
    return [
        AclEntry(
            principal_type="user",
            principal_id=f"{context.namespace.value}:{context.subject}",
        )
    ]


def parse_cursor(cursor: str | None) -> datetime | None:
    """The walk's ISO-8601 instant, or None for a first sync. A cursor this package
    cannot parse is treated as absent — the cost is one full re-list, deduplicated
    downstream by content hash, which beats refusing to sync over a formatting bug."""
    if not cursor:
        return None
    try:
        parsed = datetime.fromisoformat(cursor)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


class ProviderHttp:
    """Authenticated JSON/text/bytes calls with one shared failure classification.

    The token is fetched per request from the `TokenSource`, so a refresh that
    happened mid-sync is picked up by the next call instead of failing the rest of
    the walk with a stale header.
    """

    __slots__ = ("_client", "_token")

    def __init__(self, token: TokenSource, client: httpx.AsyncClient) -> None:
        self._token = token
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        token = await self._token.access_token()
        merged = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if headers:
            merged.update(headers)
        try:
            response = await self._client.request(
                method, url, params=params, headers=merged, json=json_body
            )
        except httpx.TimeoutException as error:
            raise ProviderApiError("provider request timed out", transient=True) from error
        except httpx.HTTPError as error:
            raise ProviderApiError("provider request failed to complete", transient=True) from error

        if response.status_code in (401, 403):
            raise ProviderAuthError("the provider no longer honours this token")
        if response.status_code == 429:
            raise ProviderApiError(
                "the provider rate-limited this sync",
                transient=True,
                retry_after=_retry_after_seconds(response),
            )
        if response.status_code >= 500:
            raise ProviderApiError("the provider failed the request", transient=True)
        if response.status_code >= 400:
            raise ProviderApiError(
                f"the provider rejected the request (HTTP {response.status_code})",
                transient=False,
            )
        return response

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self.request("GET", url, params=params, headers=headers)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ProviderApiError("the provider's response was not an object", transient=False)
        return payload

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        response = await self.request("GET", url, params=params, headers=headers)
        return response.text
