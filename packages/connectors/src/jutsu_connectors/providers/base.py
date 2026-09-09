"""Shared plumbing for live provider connectors.

Three decisions live here so ten connectors cannot make them ten ways:

* **Failure taxonomy.** A provider call fails as `ProviderApiError(transient=...)` —
  429/5xx/network are transient (the job's retry ladder handles them, honouring
  Retry-After via the scheduler's backoff), any other 4xx is permanent (rejected
  identically every time), and a dead grant is `ProviderAuthError`, its own type because
  the operator action is different: the *grant* died, not the request.

  **401 is always a dead grant; 403 is not.** GitHub and Google both answer 403 when
  throttling, so classifying every 403 as `ProviderAuthError` permanently failed a busy
  first sync *and* flipped the employee's connection to "reconnect" — an account that
  never stopped working. `_is_throttled` reads the evidence a throttle carries and a
  revocation does not.
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
from typing import Any, Final, Protocol

import httpx
from jutsu_core.models import AclEntry, SourceSystem

__all__ = [
    "DocumentGone",
    "ListingIncomplete",
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


class DocumentGone(RuntimeError):
    """The provider has no document at this identifier.

    Distinct from every other provider refusal because it is not a refusal: nothing
    is wrong, the thing simply is not there. Listing and fetching are two calls with
    a gap between them, and a document can be deleted, unshared or moved inside it.
    GitHub makes the case unavoidable — the repository listing says nothing about
    whether a repository has a README, so a connector that lists one for each
    repository must be able to say "there wasn't one" without that being an error.

    Treated as a permanently failed job it produced exactly that: a row per
    README-less repository, failed for ever, in the view an administrator reads to
    find real problems.
    """


class ListingIncomplete(RuntimeError):
    """The page budget ran out before the provider ran out of results.

    **The bug this exists to make impossible was silence.** Every listing loop here is
    bounded — an unbounded `while nextPageToken` against a hostile or looping API is a
    walk that never returns — and each one used to reach its bound by simply falling out
    of a `for ... in range(...)`, which is indistinguishable from the provider having no
    more pages. The walk then advanced `sources.last_sync_cursor` to the moment the walk
    *started*, so every document past the bound was filtered out of the next listing by
    that very cursor and never seen again. A first sync of a large mailbox indexed the
    newest few thousand messages, reported success, and lost the rest permanently.

    Raising instead means the walk keeps everything it enqueued and refuses to claim
    coverage it does not have: `run_source_walk` leaves the cursor where it was and
    records the source as incompletely listed.

    `listed` is a count, never an identifier — this message is stored and logged.
    """

    def __init__(self, listed: int, *, pages: int):
        super().__init__(
            f"the provider had more results after {pages} pages ({listed} listed); "
            "the cursor was not advanced"
        )
        self.listed = listed
        self.pages = pages


class ProviderApiError(RuntimeError):
    """The provider's API refused or failed a call.

    `transient` decides the job's fate: True retries under the ladder, False fails
    permanently. `retry_after` is advisory (seconds) when the provider sent one.
    The message never carries a response body — provider errors quote the request,
    and the request carries the token.
    """

    def __init__(
        self,
        message: str,
        *,
        transient: bool,
        retry_after: float | None = None,
        status: int | None = None,
    ):
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after
        #: The HTTP status, when there was one. Carried as a field because callers
        #: need to branch on it — GitHub has to tell a missing README (404) from a
        #: revoked grant (403) — and parsing it back out of the message is the kind
        #: of thing that works until somebody rewords the message.
        self.status = status


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


#: Words a provider uses in a 403 body when it means "slow down" rather than "go away".
#:
#: Google returns `error.errors[].reason` of `rateLimitExceeded`, `userRateLimitExceeded`
#: or `quotaExceeded`; GitHub returns a `message` containing "API rate limit exceeded" or
#: "secondary rate limit". Matched case-insensitively against the whole body because the
#: shape differs per provider and the words do not.
_THROTTLE_WORDS: Final = (
    "ratelimitexceeded",
    "userratelimitexceeded",
    "quotaexceeded",
    "rate limit exceeded",
    "secondary rate limit",
    "too many requests",
)


def _is_throttled(response: httpx.Response) -> bool:
    """Whether a 403 is a rate limit rather than a dead grant.

    **This distinction is the difference between a sync that resumes and an employee told
    to reconnect a working account.** A 403 used to be classified unconditionally as
    `ProviderAuthError`, which is permanent and fires `mark_reauth_required` — but GitHub
    and Google both answer 403 when throttling, so a busy first sync flipped the
    connection to "reconnect" and stopped retrying work that would have succeeded in a
    minute.

    Three signals, cheapest first. Any one of them is enough: a provider that sends a
    `Retry-After` on a 403 is telling us when to come back, and a grant that is gone does
    not come back.
    """
    if response.headers.get("Retry-After") is not None:
        return True
    if response.headers.get("X-RateLimit-Remaining") == "0":
        return True
    try:
        # Bounded: a hostile or broken provider must not make this read a large body into
        # memory to answer a yes/no question about a header-sized fact.
        body = response.text[:4096].lower()
    except Exception:  # a body that cannot be decoded says nothing either way
        return False
    return any(word in body for word in _THROTTLE_WORDS)


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

        # 401 is always a dead token. 403 is not: GitHub and Google both use it to
        # throttle, and treating a throttle as a revoked grant permanently fails the job
        # AND tells the employee to reconnect an account that never stopped working.
        if response.status_code == 401:
            raise ProviderAuthError("the provider no longer honours this token")
        if response.status_code == 403:
            if _is_throttled(response):
                raise ProviderApiError(
                    "the provider rate-limited this sync",
                    transient=True,
                    retry_after=_retry_after_seconds(response),
                )
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
                status=response.status_code,
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
