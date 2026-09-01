"""Google connectors against scripted Google APIs. No test may reach a network."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from jutsu_connectors.providers.base import (
    ProviderApiError,
    ProviderAuthError,
    ProviderContext,
)
from jutsu_connectors.providers.google import (
    GmailConnector,
    GoogleCalendarConnector,
    GoogleDriveConnector,
    GoogleMeetConnector,
)
from jutsu_core.models import SourceSystem

#: One Google account, one OIDC sub, shared by all four products (ADR 0014).
CONTEXT = ProviderContext(namespace=SourceSystem.GMAIL, subject="103254558086958180077")
OWNER_PRINCIPAL = "gmail:103254558086958180077"
CURSOR = "2026-08-01T00:00:00+00:00"


class StaticToken:
    def __init__(self, value: str = "goog-token") -> None:
        self.value = value

    async def access_token(self) -> str:
        return self.value


def client_over(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------------------- gmail


def gmail_raw(*, subject: str, body: str, date: str) -> str:
    """A tiny real RFC-822 message, built with explicit CRLF (ADR 0008 — never a
    committed fixture file), base64url-encoded the way Gmail's `format=raw` returns it.
    Padding is stripped to exercise the decoder's padding fix."""
    lines = [
        "Message-ID: <sync-check-001@mail.example.com>",
        "From: Priya Sharma <priya.sharma@example.com>",
        "To: Dev Kapoor <dev.kapoor@example.com>",
        f"Date: {date}",
        f"Subject: {subject}",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
    ]
    data = "\r\n".join(lines).encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


GMAIL_RAW = gmail_raw(
    subject="Q3 retro notes",
    body="The retry ladder held. Next quarter we tune the backoff.",
    date="Tue, 25 Aug 2026 09:30:00 +0000",
)


def gmail_scripted(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/gmail/v1/users/me/messages":
        if request.url.params.get("pageToken") == "gm-page-2":
            return httpx.Response(200, json={"messages": [{"id": "18c2f3a9d4e5f607"}]})
        return httpx.Response(
            200,
            json={
                "messages": [{"id": "18c2f3a9d4e5f601"}, {"id": "18c2f3a9d4e5f602"}],
                "nextPageToken": "gm-page-2",
            },
        )
    if path == "/gmail/v1/users/me/messages/18c2f3a9d4e5f601":
        assert request.url.params.get("format") == "raw"
        return httpx.Response(200, json={"raw": GMAIL_RAW, "threadId": "18c2f3a9d4e5f601"})
    if path == "/gmail/v1/users/me/messages/deadbeef00000000":
        junk = base64.urlsafe_b64encode(b"\x00\x01\x02 not mail at all").decode("ascii")
        return httpx.Response(200, json={"raw": junk, "threadId": "deadbeef00000000"})
    raise AssertionError(f"unexpected call: {path}")


class TestGmailListing:
    async def test_lists_message_ids_across_pages(self) -> None:
        async with client_over(gmail_scripted) as client:
            connector = GmailConnector(CONTEXT, StaticToken(), client)
            ids = [i async for i in connector.list_since(None)]
        assert ids == ["msg:18c2f3a9d4e5f601", "msg:18c2f3a9d4e5f602", "msg:18c2f3a9d4e5f607"]

    async def test_the_after_filter_is_forwarded_only_with_a_cursor(self) -> None:
        queries: list[str | None] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/gmail/v1/users/me/messages":
                queries.append(request.url.params.get("q"))
            return gmail_scripted(request)

        async with client_over(recording) as client:
            connector = GmailConnector(CONTEXT, StaticToken(), client)
            _ = [i async for i in connector.list_since(None)]
            _ = [i async for i in connector.list_since(CURSOR)]

        epoch = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp())
        assert queries[:2] == [None, None]
        assert queries[2:] == [f"after:{epoch}", f"after:{epoch}"]


class TestGmailFetch:
    async def test_a_raw_message_decodes_and_normalises(self) -> None:
        async with client_over(gmail_scripted) as client:
            connector = GmailConnector(CONTEXT, StaticToken(), client)
            doc = await connector.fetch("msg:18c2f3a9d4e5f601")
        assert doc.title == "Q3 retro notes"
        assert "retry ladder held" in doc.body
        assert doc.thread_id == "gmail:18c2f3a9d4e5f601"
        assert doc.created_at == datetime(2026, 8, 25, 9, 30, tzinfo=UTC)
        assert [a.principal_id for a in doc.acls] == [OWNER_PRINCIPAL]
        assert all(a.permission == "read" for a in doc.acls)
        # Gmail names the sender by email address, and an email is not a subject
        # (ADR 0014) — so it stays message content, never an author id.
        assert doc.author_external_id is None

    async def test_a_message_the_parser_rejects_is_permanent(self) -> None:
        async with client_over(gmail_scripted) as client:
            connector = GmailConnector(CONTEXT, StaticToken(), client)
            with pytest.raises(ProviderApiError) as excinfo:
                await connector.fetch("msg:deadbeef00000000")
        assert excinfo.value.transient is False


# ------------------------------------------------------------------------------- drive

DRIVE_DOC = {
    "id": "1xK9mQ2pR7vT4wZ8yB3nH6jL0aC5eD1fG",
    "name": "Incident postmortem 2026-08-14",
    "mimeType": "application/vnd.google-apps.document",
    "createdTime": "2026-07-01T08:00:00Z",
    "modifiedTime": "2026-08-14T16:20:00Z",
    "webViewLink": "https://docs.google.com/document/d/1xK9mQ2pR7vT4wZ8yB3nH6jL0aC5eD1fG/edit",
}
DRIVE_TEXT_FILE = {
    "id": "0B7qV5sN2mX1kY3jW9tR4uL6oP8aE2cI5",
    "name": "runbook.md",
    "mimeType": "text/markdown",
    "createdTime": "2026-06-10T11:00:00Z",
    "modifiedTime": "2026-08-20T09:05:00Z",
    "webViewLink": "https://drive.google.com/file/d/0B7qV5sN2mX1kY3jW9tR4uL6oP8aE2cI5/view",
}


def drive_scripted(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/drive/v3/files":
        if request.url.params.get("pageToken") == "dr-page-2":
            return httpx.Response(200, json={"files": [DRIVE_TEXT_FILE]})
        return httpx.Response(200, json={"files": [DRIVE_DOC], "nextPageToken": "dr-page-2"})
    if path == f"/drive/v3/files/{DRIVE_DOC['id']}/export":
        assert request.url.params.get("mimeType") == "text/plain"
        return httpx.Response(200, text="Root cause: the lease expired mid-claim.")
    if path == f"/drive/v3/files/{DRIVE_DOC['id']}":
        return httpx.Response(200, json=DRIVE_DOC)
    if path == f"/drive/v3/files/{DRIVE_TEXT_FILE['id']}":
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, text="# Runbook\nRestart the worker, then re-claim.")
        return httpx.Response(200, json=DRIVE_TEXT_FILE)
    raise AssertionError(f"unexpected call: {path}")


class TestDriveListing:
    async def test_lists_files_across_pages_with_the_trashed_filter(self) -> None:
        queries: list[str | None] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/drive/v3/files":
                queries.append(request.url.params.get("q"))
            return drive_scripted(request)

        async with client_over(recording) as client:
            connector = GoogleDriveConnector(CONTEXT, StaticToken(), client)
            ids = [i async for i in connector.list_since(None)]
        assert ids == [f"file:{DRIVE_DOC['id']}", f"file:{DRIVE_TEXT_FILE['id']}"]
        assert queries[0] is not None
        assert "trashed = false" in queries[0]
        assert "modifiedTime" not in queries[0]

    async def test_the_cursor_appends_a_modified_time_clause(self) -> None:
        queries: list[str | None] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/drive/v3/files":
                queries.append(request.url.params.get("q"))
            return drive_scripted(request)

        async with client_over(recording) as client:
            connector = GoogleDriveConnector(CONTEXT, StaticToken(), client)
            _ = [i async for i in connector.list_since(CURSOR)]
        assert queries[0] is not None
        assert queries[0].endswith(f"and modifiedTime > '{CURSOR}'")


class TestDriveFetch:
    async def test_a_google_doc_exports_as_plain_text(self) -> None:
        async with client_over(drive_scripted) as client:
            connector = GoogleDriveConnector(CONTEXT, StaticToken(), client)
            doc = await connector.fetch(f"file:{DRIVE_DOC['id']}")
        assert doc.title == "Incident postmortem 2026-08-14"
        assert doc.body == "Root cause: the lease expired mid-claim."
        assert doc.mime == "text/plain"
        assert doc.uri == DRIVE_DOC["webViewLink"]
        assert doc.thread_id is None
        assert doc.created_at == datetime(2026, 7, 1, 8, tzinfo=UTC)
        assert doc.modified_at == datetime(2026, 8, 14, 16, 20, tzinfo=UTC)
        assert [a.principal_id for a in doc.acls] == [OWNER_PRINCIPAL]
        assert all(a.permission == "read" for a in doc.acls)

    async def test_a_text_file_downloads_as_media(self) -> None:
        async with client_over(drive_scripted) as client:
            connector = GoogleDriveConnector(CONTEXT, StaticToken(), client)
            doc = await connector.fetch(f"file:{DRIVE_TEXT_FILE['id']}")
        assert doc.body == "# Runbook\nRestart the worker, then re-claim."
        assert doc.raw_metadata["source_mime"] == "text/markdown"


# ---------------------------------------------------------------------------- calendar

CALENDAR_EVENT = {
    "id": "7qk3n9v2j8m1p5r0_20260827T093000Z",
    "status": "confirmed",
    "summary": "Weekly platform sync",
    "description": "Agenda: retry ladder rollout, lease telemetry.",
    "htmlLink": "https://www.google.com/calendar/event?eid=N3FrM245djJqOG0xcDVyMA",
    "created": "2026-08-10T09:00:00Z",
    "updated": "2026-08-29T11:30:00Z",
    "recurringEventId": "7qk3n9v2j8m1p5r0",
    "organizer": {"email": "priya.sharma@example.com", "displayName": "Priya Sharma"},
    "attendees": [
        {"email": "dev.kapoor@example.com", "displayName": "Dev Kapoor"},
        {"email": "ops-team@example.com"},
    ],
    "start": {"dateTime": "2026-08-27T09:30:00Z"},
    "end": {"dateTime": "2026-08-27T10:00:00Z"},
}
CANCELLED_EVENT = {"id": "cancelled-standup-20260828", "status": "cancelled"}


def calendar_scripted(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/calendar/v3/calendars/primary/events":
        if request.url.params.get("pageToken") == "ca-page-2":
            return httpx.Response(200, json={"items": [CANCELLED_EVENT]})
        return httpx.Response(200, json={"items": [CALENDAR_EVENT], "nextPageToken": "ca-page-2"})
    if path == f"/calendar/v3/calendars/primary/events/{CALENDAR_EVENT['id']}":
        return httpx.Response(200, json=CALENDAR_EVENT)
    raise AssertionError(f"unexpected call: {path}")


class TestCalendarListing:
    async def test_lists_events_across_pages_skipping_cancelled(self) -> None:
        async with client_over(calendar_scripted) as client:
            connector = GoogleCalendarConnector(CONTEXT, StaticToken(), client)
            ids = [i async for i in connector.list_since(None)]
        assert ids == [f"event:{CALENDAR_EVENT['id']}"]

    async def test_updated_min_is_forwarded_only_with_a_cursor(self) -> None:
        seen: list[str | None] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/calendar/v3/calendars/primary/events":
                seen.append(request.url.params.get("updatedMin"))
            return calendar_scripted(request)

        async with client_over(recording) as client:
            connector = GoogleCalendarConnector(CONTEXT, StaticToken(), client)
            _ = [i async for i in connector.list_since(None)]
            _ = [i async for i in connector.list_since(CURSOR)]
        assert seen[:2] == [None, None]
        assert seen[2:] == [CURSOR, CURSOR]


class TestCalendarFetch:
    async def test_an_event_composes_body_and_threads_on_the_recurrence(self) -> None:
        async with client_over(calendar_scripted) as client:
            connector = GoogleCalendarConnector(CONTEXT, StaticToken(), client)
            doc = await connector.fetch(f"event:{CALENDAR_EVENT['id']}")
        assert doc.title == "Weekly platform sync"
        assert "retry ladder rollout" in doc.body
        assert "Organizer: Priya Sharma" in doc.body
        assert "Dev Kapoor" in doc.body
        # An attendee email inside the body is content, not a grant (ADR 0014).
        assert "ops-team@example.com" in doc.body
        assert doc.thread_id == "gcal:7qk3n9v2j8m1p5r0"
        assert doc.created_at == datetime(2026, 8, 10, 9, tzinfo=UTC)
        assert doc.modified_at == datetime(2026, 8, 29, 11, 30, tzinfo=UTC)
        assert doc.raw_metadata["start"] == {"dateTime": "2026-08-27T09:30:00Z"}
        assert doc.raw_metadata["end"] == {"dateTime": "2026-08-27T10:00:00Z"}
        assert [a.principal_id for a in doc.acls] == [OWNER_PRINCIPAL]
        assert all(a.permission == "read" for a in doc.acls)


# -------------------------------------------------------------------------------- meet

MEET_RECORD = {
    "name": "conferenceRecords/a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "startTime": "2026-08-28T10:00:00Z",
    "endTime": "2026-08-28T10:45:00Z",
    "space": {"name": "spaces/jQzKpXvT", "meetingCode": "abc-defg-hij"},
}
OLD_MEET_RECORD = {
    "name": "conferenceRecords/00000000-1111-2222-3333-444444444444",
    "startTime": "2026-07-15T09:00:00Z",
    "endTime": "2026-07-15T09:30:00Z",
}
SILENT_MEET_RECORD = {
    "name": "conferenceRecords/99999999-8888-7777-6666-555555555555",
    "startTime": "2026-08-30T14:00:00Z",
    "endTime": "2026-08-30T14:20:00Z",
    "space": {"name": "spaces/mRtWqYuI"},
}


def meet_scripted(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v2/conferenceRecords":
        if request.url.params.get("pageToken") == "me-page-2":
            return httpx.Response(200, json={"conferenceRecords": [OLD_MEET_RECORD]})
        return httpx.Response(
            200, json={"conferenceRecords": [MEET_RECORD], "nextPageToken": "me-page-2"}
        )
    if path == f"/v2/{MEET_RECORD['name']}":
        return httpx.Response(200, json=MEET_RECORD)
    if path == f"/v2/{MEET_RECORD['name']}/transcripts":
        return httpx.Response(
            200,
            json={
                "transcripts": [
                    {"name": f"{MEET_RECORD['name']}/transcripts/t-001", "state": "ENDED"}
                ]
            },
        )
    if path == f"/v2/{MEET_RECORD['name']}/transcripts/t-001/entries":
        if request.url.params.get("pageToken") == "en-page-2":
            return httpx.Response(
                200,
                json={
                    "transcriptEntries": [
                        {
                            "participant": f"{MEET_RECORD['name']}/participants/p2",
                            "text": "Agreed, shipping it Friday.",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "transcriptEntries": [
                    {
                        "participant": f"{MEET_RECORD['name']}/participants/p1",
                        "text": "Let us walk through the retry ladder.",
                    }
                ],
                "nextPageToken": "en-page-2",
            },
        )
    if path == f"/v2/{SILENT_MEET_RECORD['name']}":
        return httpx.Response(200, json=SILENT_MEET_RECORD)
    if path == f"/v2/{SILENT_MEET_RECORD['name']}/transcripts":
        return httpx.Response(200, json={})
    raise AssertionError(f"unexpected call: {path}")


class TestMeetListing:
    async def test_lists_conference_records_across_pages(self) -> None:
        sizes: list[str | None] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/conferenceRecords":
                sizes.append(request.url.params.get("pageSize"))
            return meet_scripted(request)

        async with client_over(recording) as client:
            connector = GoogleMeetConnector(CONTEXT, StaticToken(), client)
            ids = [i async for i in connector.list_since(None)]
        assert ids == [f"conf:{MEET_RECORD['name']}", f"conf:{OLD_MEET_RECORD['name']}"]
        assert sizes == ["50", "50"]

    async def test_the_cursor_filters_client_side(self) -> None:
        filters: list[str | None] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/conferenceRecords":
                filters.append(request.url.params.get("filter"))
            return meet_scripted(request)

        async with client_over(recording) as client:
            connector = GoogleMeetConnector(CONTEXT, StaticToken(), client)
            ids = [i async for i in connector.list_since(CURSOR)]
        assert ids == [f"conf:{MEET_RECORD['name']}"]
        # No server-side filter parameter exists to forward, so none may be sent.
        assert filters == [None, None]


class TestMeetFetch:
    async def test_a_conference_with_a_transcript_joins_entries_in_order(self) -> None:
        async with client_over(meet_scripted) as client:
            connector = GoogleMeetConnector(CONTEXT, StaticToken(), client)
            doc = await connector.fetch(f"conf:{MEET_RECORD['name']}")
        assert doc.body == (
            f"{MEET_RECORD['name']}/participants/p1: Let us walk through the retry ladder.\n"
            f"{MEET_RECORD['name']}/participants/p2: Agreed, shipping it Friday."
        )
        assert doc.title == "Meet abc-defg-hij"
        assert doc.thread_id == "meet:abc-defg-hij"
        assert doc.created_at == datetime(2026, 8, 28, 10, tzinfo=UTC)
        assert doc.raw_metadata["has_transcript"] is True
        assert [a.principal_id for a in doc.acls] == [OWNER_PRINCIPAL]
        assert all(a.permission == "read" for a in doc.acls)

    async def test_a_conference_without_a_transcript_gets_a_metadata_body(self) -> None:
        async with client_over(meet_scripted) as client:
            connector = GoogleMeetConnector(CONTEXT, StaticToken(), client)
            doc = await connector.fetch(f"conf:{SILENT_MEET_RECORD['name']}")
        assert "2026-08-30T14:00:00Z" in doc.body
        assert "2026-08-30T14:20:00Z" in doc.body
        assert "no transcript" in doc.body
        assert doc.thread_id is None
        assert doc.raw_metadata["has_transcript"] is False


# ---------------------------------------------------------------------------- id shapes


class TestIdShapes:
    async def test_an_unrecognised_id_shape_is_permanent_on_every_connector(self) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a bad id shape must fail before any HTTP call")

        cases: list[tuple[type, str]] = [
            (GmailConnector, "file:1xK9mQ2pR7vT4wZ8yB3nH6jL0aC5eD1fG"),
            (GoogleDriveConnector, "msg:18c2f3a9d4e5f601"),
            (GoogleCalendarConnector, "conf:conferenceRecords/a1b2c3d4"),
            (GoogleMeetConnector, "event:7qk3n9v2j8m1p5r0"),
            (GoogleMeetConnector, "conf:not-a-conference-record-name"),
        ]
        async with client_over(unreachable) as client:
            for connector_class, bad_id in cases:
                connector = connector_class(CONTEXT, StaticToken(), client)
                with pytest.raises(ProviderApiError) as excinfo:
                    await connector.fetch(bad_id)
                assert excinfo.value.transient is False


# ----------------------------------------------------------------------- failure taxonomy


class TestFailureTaxonomy:
    async def test_a_rate_limit_is_transient_and_carries_retry_after(self) -> None:
        def limited(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "30"}, json={})

        async with client_over(limited) as client:
            connector = GmailConnector(CONTEXT, StaticToken(), client)
            with pytest.raises(ProviderApiError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert excinfo.value.transient is True
        assert excinfo.value.retry_after == 30.0

    async def test_a_dead_token_is_an_auth_error_not_a_retry(self) -> None:
        def unauthorized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED"}})

        async with client_over(unauthorized) as client:
            connector = GoogleDriveConnector(CONTEXT, StaticToken(), client)
            with pytest.raises(ProviderAuthError):
                _ = [i async for i in connector.list_since(None)]

    async def test_no_error_text_ever_carries_the_token(self) -> None:
        def unauthorized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED"}})

        async with client_over(unauthorized) as client:
            connector = GoogleMeetConnector(CONTEXT, StaticToken(), client)
            with pytest.raises(ProviderAuthError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert "goog-token" not in json.dumps(str(excinfo.value))
