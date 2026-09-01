"""ZoomConnector against a scripted api.zoom.us. No test may reach a network."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from jutsu_connectors.providers.base import ProviderApiError, ProviderContext
from jutsu_connectors.providers.zoom import ZoomConnector, _encoded_uuid, _vtt_to_text
from jutsu_core.models import SourceSystem

CONTEXT = ProviderContext(namespace=SourceSystem.ZOOM, subject="z8yAAAAAxyz")


class StaticToken:
    def __init__(self, value: str = "zoom-token") -> None:
        self.value = value

    async def access_token(self) -> str:
        return self.value


def connector_over(handler: Any) -> tuple[ZoomConnector, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ZoomConnector(CONTEXT, StaticToken(), client), client


VTT = "\r\n".join(
    [
        "WEBVTT",
        "",
        "1",
        "00:00:01.000 --> 00:00:04.000",
        "Priya Sharma: The retry ladder honours Retry-After now.",
        "",
        "2",
        "00:00:04.500 --> 00:00:07.000",
        "Arjun Mehta: Then the queue drains itself after a 429 storm.",
    ]
)

RECORDING: dict[str, Any] = {
    "uuid": "AAAbbbCCC123==",
    "id": 987654321,
    "topic": "Weekly ingestion sync",
    "start_time": "2026-08-20T09:00:00Z",
    "duration": 42,
    "host_id": "z8yAAAAAxyz",
    "share_url": "https://zoom.us/rec/share/abc",
    "recording_files": [
        {"file_type": "MP4", "download_url": "https://zoom.us/rec/download/video"},
        {"file_type": "TRANSCRIPT", "download_url": "https://zoom.us/rec/download/vtt"},
    ],
}


class TestListSince:
    @pytest.mark.asyncio
    async def test_lists_recordings_across_windows_and_pages(self) -> None:
        """A 180-day first sync walks 30-day windows; pages follow next_page_token."""
        calls: list[dict[str, list[str]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v2/users/me/recordings"
            params = parse_qs(urlparse(str(request.url)).query)
            calls.append(params)
            if params.get("next_page_token") == ["page2"]:
                return httpx.Response(
                    200, json={"meetings": [{"uuid": "second=="}], "next_page_token": ""}
                )
            if len(calls) == 1:
                return httpx.Response(
                    200, json={"meetings": [{"uuid": "first=="}], "next_page_token": "page2"}
                )
            return httpx.Response(200, json={"meetings": [], "next_page_token": ""})

        connector, client = connector_over(handler)
        try:
            listed = [item async for item in connector.list_since(None)]
        finally:
            await client.aclose()

        assert listed[:2] == ["recording:first==", "recording:second=="]
        # 180 days at 30-day windows: six windows infixed with one paged continuation.
        assert len(calls) == 7
        for params in calls:
            window = (
                datetime.fromisoformat(params["to"][0]) - datetime.fromisoformat(params["from"][0])
            ).days
            assert window <= 30

    @pytest.mark.asyncio
    async def test_cursor_narrows_the_walk(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = parse_qs(urlparse(str(request.url)).query)
            seen.append(params["from"][0])
            return httpx.Response(200, json={"meetings": []})

        cursor = datetime.now(tz=UTC).isoformat()
        connector, client = connector_over(handler)
        try:
            assert [item async for item in connector.list_since(cursor)] == []
        finally:
            await client.aclose()
        assert len(seen) == 1  # today-to-today: one window, no history walk


class TestFetch:
    @pytest.mark.asyncio
    async def test_transcript_becomes_the_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/recordings"):
                return httpx.Response(200, json=RECORDING)
            if request.url.path == "/rec/download/vtt":
                return httpx.Response(200, text=VTT)
            raise AssertionError(f"unexpected call {request.url}")

        connector, client = connector_over(handler)
        try:
            document = await connector.fetch("recording:AAAbbbCCC123==")
        finally:
            await client.aclose()

        assert document.source_system is SourceSystem.ZOOM
        assert document.title == "Weekly ingestion sync"
        assert "Priya Sharma: The retry ladder honours Retry-After now." in document.body
        assert "-->" not in document.body
        assert "WEBVTT" not in document.body
        assert document.author_external_id == "zoom:z8yAAAAAxyz"
        assert document.thread_id == "zoom:987654321"
        assert document.created_at == datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
        assert document.acls[0].principal_id == "zoom:z8yAAAAAxyz"
        assert document.raw_metadata["has_transcript"] is True

    @pytest.mark.asyncio
    async def test_missing_transcript_yields_honest_metadata_body(self) -> None:
        stripped = {**RECORDING, "recording_files": [RECORDING["recording_files"][0]]}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=stripped)

        connector, client = connector_over(handler)
        try:
            document = await connector.fetch("recording:AAAbbbCCC123==")
        finally:
            await client.aclose()
        assert "No transcript was available" in document.body
        assert "42 minutes" in document.body
        assert document.raw_metadata["has_transcript"] is False

    @pytest.mark.asyncio
    async def test_unknown_shape_is_permanent(self) -> None:
        connector, client = connector_over(lambda request: httpx.Response(200, json={}))
        try:
            with pytest.raises(ProviderApiError) as excinfo:
                await connector.fetch("meeting:123")
        finally:
            await client.aclose()
        assert excinfo.value.transient is False

    @pytest.mark.asyncio
    async def test_slashed_uuid_is_double_encoded_in_the_path(self) -> None:
        """Zoom 404s single-encoded UUIDs that start with / or contain // — the quirk
        the module docstring records. The path must carry %25, the encoded %."""
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.raw_path.decode("ascii"))
            return httpx.Response(200, json={**RECORDING, "recording_files": []})

        connector, client = connector_over(handler)
        try:
            await connector.fetch("recording://odd//uuid==")
        finally:
            await client.aclose()
        assert "%252F" in paths[0]
        assert "//" not in paths[0].removeprefix("/v2/meetings/").removesuffix("/recordings")


class TestHelpers:
    def test_encoded_uuid_single_for_ordinary_values(self) -> None:
        assert _encoded_uuid("AAAbbbCCC123==") == "AAAbbbCCC123%3D%3D"

    def test_encoded_uuid_double_for_slash_prefixed_values(self) -> None:
        assert _encoded_uuid("/start==") == "%252Fstart%253D%253D"

    def test_vtt_to_text_keeps_speakers_and_drops_plumbing(self) -> None:
        text = _vtt_to_text(VTT)
        assert text.splitlines() == [
            "Priya Sharma: The retry ladder honours Retry-After now.",
            "Arjun Mehta: Then the queue drains itself after a 429 storm.",
        ]

    def test_registry_carries_zoom(self) -> None:
        from jutsu_connectors.providers import CONNECTOR_CLASSES

        assert CONNECTOR_CLASSES["zoom"] is ZoomConnector
        assert json.dumps(sorted(CONNECTOR_CLASSES))  # every key JSON-serialisable
