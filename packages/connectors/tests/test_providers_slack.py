"""SlackConnector against a scripted slack.com/api. No test may reach a network."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from jutsu_connectors.providers.base import (
    ProviderApiError,
    ProviderAuthError,
    ProviderContext,
)
from jutsu_connectors.providers.slack import SlackConnector
from jutsu_core.models import SourceSystem

CONTEXT = ProviderContext(namespace=SourceSystem.SLACK, subject="U02AB3CDE")


class StaticToken:
    def __init__(self, value: str = "slack-token") -> None:
        self.value = value
        self.calls = 0

    async def access_token(self) -> str:
        self.calls += 1
        return self.value


def connector_over(handler: Any) -> tuple[SlackConnector, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SlackConnector(CONTEXT, StaticToken(), client), client


def slack_ts(instant: datetime) -> str:
    return f"{instant.timestamp():.6f}"


ANCIENT_INSTANT = datetime(2024, 3, 5, 11, 0, tzinfo=UTC)
PARENT_INSTANT = datetime(2026, 8, 12, 9, 30, 0, 300, tzinfo=UTC)
JOIN_INSTANT = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
NOTE_INSTANT = datetime(2026, 8, 12, 10, 5, tzinfo=UTC)
REPLY_INSTANT = datetime(2026, 8, 12, 10, 15, 0, 600, tzinfo=UTC)
SHIPMENT_INSTANT = datetime(2026, 8, 20, 16, 45, tzinfo=UTC)

ANCIENT_TS = slack_ts(ANCIENT_INSTANT)
PARENT_TS = slack_ts(PARENT_INSTANT)
JOIN_TS = slack_ts(JOIN_INSTANT)
NOTE_TS = slack_ts(NOTE_INSTANT)
REPLY_TS = slack_ts(REPLY_INSTANT)
SHIPMENT_TS = slack_ts(SHIPMENT_INSTANT)

GENERAL = {"id": "C024BE91L", "name": "general", "is_channel": True, "is_archived": False}
LOGISTICS = {"id": "C05QZ7RT2", "name": "logistics", "is_channel": True, "is_archived": False}

PARENT = {
    "type": "message",
    "user": "U02AB3CDE",
    "text": "Deploy window moves to 14:00 UTC — the index rebuild overran.",
    "ts": PARENT_TS,
    "team": "T024BE7LD",
}
REPLY = {
    "type": "message",
    "user": "U03XY9ZAB",
    "text": "Rolled back; the missing partial index was the cause.",
    "ts": REPLY_TS,
    "thread_ts": PARENT_TS,
    "parent_user_id": "U02AB3CDE",
    "team": "T024BE7LD",
}
JOIN = {
    "type": "message",
    "subtype": "channel_join",
    "user": "U04QRSTUV",
    "text": "<@U04QRSTUV> has joined the channel",
    "ts": JOIN_TS,
}
APP_NOTE = {
    "type": "message",
    "bot_id": "B024BE7LH",
    "text": "Nightly build finished in 12m.",
    "ts": NOTE_TS,
}
ANCIENT = {
    "type": "message",
    "user": "U02AB3CDE",
    "text": "Kicking off the migration plan thread.",
    "ts": ANCIENT_TS,
}
SHIPMENT = {
    "type": "message",
    "user": "U02AB3CDE",
    "text": "Carrier confirmed pickup for Thursday.",
    "ts": SHIPMENT_TS,
}

HISTORY: dict[str, list[dict[str, Any]]] = {
    "C024BE91L": [REPLY, APP_NOTE, JOIN, PARENT, ANCIENT],
    "C05QZ7RT2": [SHIPMENT],
}

CHANNEL_PAGE_TWO = "dGVhbTpDMDVRWjdSVDI="
HISTORY_PAGE_TWO = "bXNnOnBhZ2UtdHdv"


def ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, **payload})


def scripted(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    params = request.url.params
    if path == "/api/users.conversations":
        assert params.get("types") == "public_channel"
        assert params.get("exclude_archived") == "true"
        assert params.get("limit") == "200"
        if params.get("cursor") == CHANNEL_PAGE_TWO:
            return ok({"channels": [LOGISTICS], "response_metadata": {"next_cursor": ""}})
        return ok({"channels": [GENERAL], "response_metadata": {"next_cursor": CHANNEL_PAGE_TWO}})
    if path == "/api/conversations.history":
        channel = params.get("channel")
        if params.get("limit") == "1":
            assert params.get("inclusive") == "true"
            assert params.get("oldest") == params.get("latest")
            wanted = params.get("latest")
            found = [m for m in HISTORY.get(channel or "", []) if m["ts"] == wanted]
            return ok({"messages": found})
        assert params.get("limit") == "200"
        if channel == "C024BE91L":
            if params.get("cursor") == HISTORY_PAGE_TWO:
                return ok({"messages": [PARENT, ANCIENT], "response_metadata": {"next_cursor": ""}})
            return ok(
                {
                    "messages": [REPLY, APP_NOTE, JOIN],
                    "response_metadata": {"next_cursor": HISTORY_PAGE_TWO},
                }
            )
        if channel == "C05QZ7RT2":
            return ok({"messages": [SHIPMENT], "response_metadata": {"next_cursor": ""}})
    if path == "/api/conversations.info":
        for chan in (GENERAL, LOGISTICS):
            if params.get("channel") == chan["id"]:
                return ok({"channel": chan})
    raise AssertionError(f"unexpected call: {path}")


class TestListing:
    async def test_lists_messages_across_channel_and_history_pages(self) -> None:
        connector, client = connector_over(scripted)
        async with client:
            ids = [i async for i in connector.list_since(None)]
        assert ids == [
            f"msg:C024BE91L:{REPLY_TS}",
            f"msg:C024BE91L:{PARENT_TS}",
            f"msg:C024BE91L:{ANCIENT_TS}",
            f"msg:C05QZ7RT2:{SHIPMENT_TS}",
        ]

    async def test_a_subtype_or_userless_message_is_never_listed(self) -> None:
        connector, client = connector_over(scripted)
        async with client:
            ids = [i async for i in connector.list_since(None)]
        assert f"msg:C024BE91L:{JOIN_TS}" not in ids
        assert f"msg:C024BE91L:{NOTE_TS}" not in ids

    async def test_a_message_older_than_the_cursor_is_never_listed(self) -> None:
        # The scripted provider ignores `oldest` entirely, so this pins the
        # client-side stop, not the server-side filter.
        connector, client = connector_over(scripted)
        async with client:
            ids = [i async for i in connector.list_since("2026-08-01T00:00:00+00:00")]
        assert f"msg:C024BE91L:{ANCIENT_TS}" not in ids
        assert f"msg:C024BE91L:{PARENT_TS}" in ids
        assert f"msg:C05QZ7RT2:{SHIPMENT_TS}" in ids

    async def test_the_cursor_is_forwarded_as_oldest_to_every_history_call(self) -> None:
        seen: list[str] = []

        def recording(request: httpx.Request) -> httpx.Response:
            is_listing = request.url.params.get("limit") != "1"
            if request.url.path == "/api/conversations.history" and is_listing:
                seen.append(request.url.params.get("oldest", ""))
            return scripted(request)

        expected = f"{datetime(2026, 8, 1, tzinfo=UTC).timestamp():.6f}"
        connector, client = connector_over(recording)
        async with client:
            _ = [i async for i in connector.list_since("2026-08-01T00:00:00+00:00")]
        assert seen == [expected, expected, expected]

    async def test_listing_caches_channel_names_for_fetch(self) -> None:
        info_calls = 0

        def counting(request: httpx.Request) -> httpx.Response:
            nonlocal info_calls
            if request.url.path == "/api/conversations.info":
                info_calls += 1
            return scripted(request)

        connector, client = connector_over(counting)
        async with client:
            _ = [i async for i in connector.list_since(None)]
            doc = await connector.fetch(f"msg:C024BE91L:{PARENT_TS}")
        assert info_calls == 0
        assert doc.title == f"#general at {PARENT_INSTANT.isoformat()}"


class TestFetch:
    async def test_a_message_normalises_with_author_thread_and_owner_acl(self) -> None:
        connector, client = connector_over(scripted)
        async with client:
            doc = await connector.fetch(f"msg:C024BE91L:{PARENT_TS}")
        assert doc.title == f"#general at {PARENT_INSTANT.isoformat()}"
        assert "index rebuild overran" in doc.body
        assert doc.author_external_id == "slack:U02AB3CDE"
        assert doc.thread_id == f"slack:C024BE91L:{PARENT_TS}"
        assert doc.created_at == PARENT_INSTANT
        assert [a.principal_id for a in doc.acls] == ["slack:U02AB3CDE"]
        assert all(a.permission == "read" for a in doc.acls)

    async def test_fetch_resolves_the_channel_name_when_not_cached(self) -> None:
        info_calls = 0

        def counting(request: httpx.Request) -> httpx.Response:
            nonlocal info_calls
            if request.url.path == "/api/conversations.info":
                info_calls += 1
            return scripted(request)

        connector, client = connector_over(counting)
        async with client:
            doc = await connector.fetch(f"msg:C05QZ7RT2:{SHIPMENT_TS}")
        assert info_calls == 1
        assert doc.title == f"#logistics at {SHIPMENT_INSTANT.isoformat()}"

    async def test_a_reply_threads_to_its_parent_ts(self) -> None:
        connector, client = connector_over(scripted)
        async with client:
            doc = await connector.fetch(f"msg:C024BE91L:{REPLY_TS}")
        assert doc.thread_id == f"slack:C024BE91L:{PARENT_TS}"
        assert doc.thread_id.endswith(PARENT_TS)
        assert doc.author_external_id == "slack:U03XY9ZAB"

    async def test_a_vanished_message_is_permanent(self) -> None:
        gone = slack_ts(datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
        connector, client = connector_over(scripted)
        async with client:
            with pytest.raises(ProviderApiError) as excinfo:
                await connector.fetch(f"msg:C024BE91L:{gone}")
        assert excinfo.value.transient is False
        assert "no longer exists" in str(excinfo.value)

    async def test_an_unrecognised_id_shape_is_permanent(self) -> None:
        connector, client = connector_over(scripted)
        async with client:
            for bad in ("channel:C024BE91L", "msg:C024BE91L:not-a-ts", f"msg::{PARENT_TS}"):
                with pytest.raises(ProviderApiError) as excinfo:
                    await connector.fetch(bad)
                assert excinfo.value.transient is False


class TestFailureTaxonomy:
    async def test_ok_false_ratelimited_is_transient(self) -> None:
        def limited(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "ratelimited"})

        connector, client = connector_over(limited)
        async with client:
            with pytest.raises(ProviderApiError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert excinfo.value.transient is True

    async def test_ok_false_token_revoked_is_an_auth_error(self) -> None:
        def revoked(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "token_revoked"})

        connector, client = connector_over(revoked)
        async with client:
            with pytest.raises(ProviderAuthError):
                _ = [i async for i in connector.list_since(None)]

    async def test_ok_false_with_an_unknown_code_is_permanent(self) -> None:
        def refused(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

        connector, client = connector_over(refused)
        async with client:
            with pytest.raises(ProviderApiError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert excinfo.value.transient is False

    async def test_a_rate_limit_is_transient_and_carries_retry_after(self) -> None:
        def limited(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "30"}, json={})

        connector, client = connector_over(limited)
        async with client:
            with pytest.raises(ProviderApiError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert excinfo.value.transient is True
        assert excinfo.value.retry_after == 30.0

    async def test_a_dead_token_is_an_auth_error_not_a_retry(self) -> None:
        def unauthorized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"ok": False, "error": "invalid_auth"})

        connector, client = connector_over(unauthorized)
        async with client:
            with pytest.raises(ProviderAuthError):
                _ = [i async for i in connector.list_since(None)]

    async def test_no_error_text_ever_carries_the_token(self) -> None:
        def revoked(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "token_revoked"})

        connector, client = connector_over(revoked)
        async with client:
            with pytest.raises(ProviderAuthError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert "slack-token" not in str(excinfo.value)
