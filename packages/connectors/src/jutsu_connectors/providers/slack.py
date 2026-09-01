"""Slack over the Web API: messages from the public channels the token's user is in.

Scopes are `channels:read` + `channels:history` only — read-only by construction
(§4.8), and public non-archived channels only: private channels, group DMs and IMs
need wider scopes plus provider-side membership expressed as subjects, which ADR 0014
does not let a fetcher guess. One document shape, with a stable external id:

    msg:{channel_id}:{ts}    one message — `ts` is Slack's identity for it

Slack reports failure in the body, not the status line: HTTP 200 carrying
`{"ok": false, "error": "<code>"}`. Every payload passes through `_unwrap`, which maps
the code onto the shared failure taxonomy. The code is an enum from Slack's error
table, never message content, so it may appear in an error message.

Honest limitation: `conversations.history` returns a thread's parent and any reply
broadcast back to the channel, but not the replies that stayed inside the thread —
those live behind `conversations.replies`, which is outside this module's walk. The
replies that do arrive still group under their parent via `thread_ts`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
from jutsu_core.models import AclEntry, RawDocument, SourceSystem

from jutsu_connectors.providers.base import (
    ProviderApiError,
    ProviderAuthError,
    ProviderContext,
    ProviderHttp,
    TokenSource,
    owner_acl,
    parse_cursor,
)

_API = "https://slack.com/api"
_PAGE_LIMIT = 200
#: Bounded pagination: a runaway listing is a provider bug amplified into a stuck
#: walk. 50 pages of 200 is far beyond any single sync and still finite.
_MAX_PAGES = 50

#: The grant died, not the request — reconnecting is the fix, so these are auth
#: errors rather than retries.
_AUTH_CODES = frozenset({"token_revoked", "token_expired", "invalid_auth", "account_inactive"})


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    """Slack's failure channel is the body: `ok=false` plus an error code."""
    if payload.get("ok") is True:
        return payload
    code = str(payload.get("error") or "unknown_error")
    if code in _AUTH_CODES:
        raise ProviderAuthError(f"slack no longer honours this token ({code})")
    if code == "ratelimited":
        raise ProviderApiError("slack rate-limited this sync (ratelimited)", transient=True)
    raise ProviderApiError(f"slack refused the call ({code})", transient=False)


def _instant_of(ts: str) -> datetime | None:
    """Slack's `ts` is epoch seconds with microsecond precision, serialised as a
    string. It is kept verbatim in external ids and only ever parsed for time."""
    try:
        seconds = float(ts)
    except ValueError:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


class SlackConnector:
    """The `Connector` protocol against slack.com/api."""

    system = SourceSystem.SLACK

    def __init__(
        self, context: ProviderContext, token: TokenSource, client: httpx.AsyncClient
    ) -> None:
        self._context = context
        self._http = ProviderHttp(token, client)
        # Channel names travel with the listing; fetch falls back to
        # `conversations.info` only when a document arrives without one.
        self._channel_names: dict[str, str] = {}

    async def _pages(self, url: str, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        cursor = ""
        for _ in range(_MAX_PAGES):
            page_params: dict[str, Any] = {**params, "limit": _PAGE_LIMIT}
            if cursor:
                page_params["cursor"] = cursor
            payload = _unwrap(await self._http.get_json(url, params=page_params))
            yield payload
            metadata = payload.get("response_metadata") or {}
            next_cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else ""
            cursor = next_cursor if isinstance(next_cursor, str) else ""
            if not cursor:
                return

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        async for payload in self._pages(
            f"{_API}/users.conversations",
            {"types": "public_channel", "exclude_archived": "true"},
        ):
            for channel in payload.get("channels") or []:
                channel_id = channel.get("id")
                if not isinstance(channel_id, str) or not channel_id:
                    continue
                name = channel.get("name")
                if isinstance(name, str) and name:
                    self._channel_names[channel_id] = name
                async for external_id in self._channel_messages(channel_id, since):
                    yield external_id

    async def _channel_messages(
        self, channel_id: str, since: datetime | None
    ) -> AsyncIterator[str]:
        params: dict[str, Any] = {"channel": channel_id}
        if since is not None:
            params["oldest"] = f"{since.timestamp():.6f}"
        async for payload in self._pages(f"{_API}/conversations.history", params):
            for message in payload.get("messages") or []:
                ts = message.get("ts")
                if not isinstance(ts, str) or not ts:
                    continue
                instant = _instant_of(ts)
                if instant is None:
                    continue
                if since is not None and instant < since:
                    # History pages newest-first, so everything past this point
                    # is older — and the guard holds even where the provider
                    # ignored `oldest`.
                    return
                if message.get("subtype") or not message.get("user"):
                    continue
                yield f"msg:{channel_id}:{ts}"

    async def fetch(self, external_id: str) -> RawDocument:
        if external_id.startswith("msg:"):
            reference = external_id.removeprefix("msg:")
            channel, _, ts = reference.partition(":")
            instant = _instant_of(ts) if ts else None
            if channel and instant is not None:
                return await self._fetch_message(channel, ts, instant)
        raise ProviderApiError("unrecognised slack external id shape", transient=False)

    async def _channel_name(self, channel: str) -> str:
        cached = self._channel_names.get(channel)
        if cached is not None:
            return cached
        payload = _unwrap(
            await self._http.get_json(f"{_API}/conversations.info", params={"channel": channel})
        )
        name = (payload.get("channel") or {}).get("name")
        resolved = name if isinstance(name, str) and name else channel
        self._channel_names[channel] = resolved
        return resolved

    async def _fetch_message(self, channel: str, ts: str, created_at: datetime) -> RawDocument:
        payload = _unwrap(
            await self._http.get_json(
                f"{_API}/conversations.history",
                params={
                    "channel": channel,
                    "latest": ts,
                    "oldest": ts,
                    "inclusive": "true",
                    "limit": 1,
                },
            )
        )
        messages = payload.get("messages") or []
        if not messages:
            raise ProviderApiError(
                "the slack message no longer exists at this instant", transient=False
            )
        message = messages[0]
        user = message.get("user")
        thread_ts = message.get("thread_ts")
        if not isinstance(thread_ts, str) or not thread_ts:
            thread_ts = ts
        edited_ts = (message.get("edited") or {}).get("ts")
        modified_at = _instant_of(edited_ts) if isinstance(edited_ts, str) else None
        name = await self._channel_name(channel)
        return RawDocument(
            external_id=f"msg:{channel}:{ts}",
            source_system=self.system,
            title=f"#{name} at {created_at.isoformat()}",
            body=str(message.get("text") or ""),
            mime="text/plain",
            author_external_id=(
                f"{self.system.value}:{user}" if isinstance(user, str) and user else None
            ),
            thread_id=f"{self.system.value}:{channel}:{thread_ts}",
            created_at=created_at,
            modified_at=modified_at,
            acls=owner_acl(self._context),
            raw_metadata={"channel": channel, "ts": ts, "thread_ts": thread_ts, "kind": "message"},
        )

    async def acls(self, external_id: str) -> list[AclEntry]:
        return owner_acl(self._context)
