"""Google Workspace over REST: Gmail mail, Drive text, Calendar events, Meet records.

Four connectors, one account. ADR 0014 maps every Google product to the `gmail` ACL
namespace because one Google account is one OIDC `sub` — so all four classes here mint
principals as `gmail:{sub}` via `owner_acl`, and their documents differ by external-id
prefix and metadata, never by namespace:

    msg:{gmail message id}
    file:{drive file id}
    event:{calendar event id}
    conf:conferenceRecords/{record}

Google reports senders, collaborators and attendees as email addresses, and an email is
not a subject (ADR 0014) — so sharing data stays content inside the body, never a grant
and never an `author_external_id`. Gmail, Drive and Calendar filter incrementally on the
server (`after:`, `modifiedTime >`, `updatedMin`); the Meet list API's filter syntax
cannot express a start-time bound usefully, so `GoogleMeetConnector` filters client-side
and a re-listed record costs one `unchanged` outcome downstream.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
from jutsu_core.models import AclEntry, RawDocument, SourceSystem

from jutsu_connectors.providers.base import (
    ProviderApiError,
    ProviderContext,
    ProviderHttp,
    TokenSource,
    owner_acl,
    parse_cursor,
)
from jutsu_connectors.rfc822 import UnparsableMessage, parse_message

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_DRIVE_API = "https://www.googleapis.com/drive/v3"
_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
_MEET_API = "https://meet.googleapis.com/v2"

_PAGE_SIZE = 100
_MEET_PAGE_SIZE = 50
#: Bounded pagination: a runaway listing is a provider bug amplified into a stuck
#: walk. 50 pages is far beyond any personal account and still finite.
_MAX_PAGES = 50

_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_DRIVE_FILE_FIELDS = "id,name,mimeType,modifiedTime,createdTime,webViewLink"
#: Text-bearing files only. Binary formats need per-format extraction this slice does
#: not do, and a base64 blob embedded as "text" would poison retrieval quietly.
_DRIVE_LIST_QUERY = (
    f"trashed = false and (mimeType = '{_GOOGLE_DOC_MIME}' or mimeType contains 'text/')"
)


def _instant(value: Any) -> datetime:
    """Google timestamps are RFC 3339, UTC with a Z suffix. A missing or malformed one
    falls back to now rather than crashing a sync over a field only ordering reads."""
    if not isinstance(value, str) or not value:
        return datetime.now(tz=UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(tz=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class _GoogleConnector:
    """Shared plumbing for the four Google surfaces.

    One constructor shape, one pagination loop, one `acls` answer — the products
    differ in endpoints and document shapes, never in how they authenticate, page,
    or grant.
    """

    system = SourceSystem.GMAIL

    def __init__(
        self, context: ProviderContext, token: TokenSource, client: httpx.AsyncClient
    ) -> None:
        self._context = context
        self._http = ProviderHttp(token, client)

    async def _pages(
        self, url: str, params: dict[str, Any], *, items: str
    ) -> AsyncIterator[list[Any]]:
        """Google's `pageToken`/`nextPageToken` walk, bounded by `_MAX_PAGES`."""
        token: str | None = None
        for _ in range(_MAX_PAGES):
            merged = dict(params)
            if token:
                merged["pageToken"] = token
            payload = await self._http.get_json(url, params=merged)
            rows = payload.get(items)
            if isinstance(rows, list) and rows:
                yield rows
            next_token = payload.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return
            token = next_token

    async def acls(self, external_id: str) -> list[AclEntry]:
        return owner_acl(self._context)


class GmailConnector(_GoogleConnector):
    """The `Connector` protocol against gmail.googleapis.com.

    Messages come down as raw RFC 822 and go through the corpus parser — but the
    document is assembled here, not by `to_raw_document`/`acls_for`: those mint
    email-address principals, which are not subjects in the `gmail` namespace
    (ADR 0014). Sender and recipients remain visible as message content.
    """

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        params: dict[str, Any] = {"maxResults": _PAGE_SIZE}
        if since is not None:
            # Gmail's `after:` operator takes epoch seconds, not an ISO instant.
            params["q"] = f"after:{int(since.timestamp())}"
        async for messages in self._pages(
            f"{_GMAIL_API}/users/me/messages", params, items="messages"
        ):
            for message in messages:
                identifier = message.get("id")
                if isinstance(identifier, str) and identifier:
                    yield f"msg:{identifier}"

    async def fetch(self, external_id: str) -> RawDocument:
        if not external_id.startswith("msg:"):
            raise ProviderApiError("unrecognised gmail external id shape", transient=False)
        message_id = external_id.removeprefix("msg:")
        payload = await self._http.get_json(
            f"{_GMAIL_API}/users/me/messages/{message_id}", params={"format": "raw"}
        )
        raw = payload.get("raw")
        if not isinstance(raw, str) or not raw:
            raise ProviderApiError("gmail returned a message without raw content", transient=False)
        try:
            data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except ValueError as error:
            raise ProviderApiError(
                "gmail raw content was not valid base64url", transient=False
            ) from error
        try:
            parsed = parse_message(data)
        except UnparsableMessage as error:
            raise ProviderApiError(
                "gmail message could not be parsed as mail", transient=False
            ) from error

        thread = payload.get("threadId")
        return RawDocument(
            external_id=external_id,
            source_system=self.system,
            title=parsed.subject or "(no subject)",
            body=parsed.body,
            mime=parsed.body_mime,
            thread_id=f"gmail:{thread}" if isinstance(thread, str) and thread else None,
            created_at=parsed.sent_at or datetime.now(tz=UTC),
            acls=owner_acl(self._context),
            raw_metadata={
                "kind": "gmail_message",
                "message_id": parsed.message_id,
                "defects": list(parsed.defects),
                "date_from_header": parsed.sent_at is not None,
            },
        )


class GoogleDriveConnector(_GoogleConnector):
    """The `Connector` protocol against www.googleapis.com/drive/v3.

    Google Docs export as plain text; other text files download as they are. Drive's
    sharing metadata names email addresses, so it cannot widen a grant (ADR 0014) and
    is not fetched at all.
    """

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        query = _DRIVE_LIST_QUERY
        if since is not None:
            query += f" and modifiedTime > '{since.isoformat()}'"
        params = {
            "q": query,
            "fields": f"nextPageToken, files({_DRIVE_FILE_FIELDS})",
            "pageSize": _PAGE_SIZE,
        }
        async for files in self._pages(f"{_DRIVE_API}/files", params, items="files"):
            for file in files:
                identifier = file.get("id")
                if isinstance(identifier, str) and identifier:
                    yield f"file:{identifier}"

    async def fetch(self, external_id: str) -> RawDocument:
        if not external_id.startswith("file:"):
            raise ProviderApiError("unrecognised google drive external id shape", transient=False)
        file_id = external_id.removeprefix("file:")
        meta = await self._http.get_json(
            f"{_DRIVE_API}/files/{file_id}", params={"fields": _DRIVE_FILE_FIELDS}
        )
        source_mime = meta.get("mimeType")
        if source_mime == _GOOGLE_DOC_MIME:
            body = await self._http.get_text(
                f"{_DRIVE_API}/files/{file_id}/export", params={"mimeType": "text/plain"}
            )
        else:
            body = await self._http.get_text(
                f"{_DRIVE_API}/files/{file_id}", params={"alt": "media"}
            )
        return RawDocument(
            external_id=external_id,
            source_system=self.system,
            uri=meta.get("webViewLink"),
            title=str(meta.get("name") or file_id),
            body=body,
            mime="text/plain",
            thread_id=None,
            created_at=_instant(meta.get("createdTime")),
            modified_at=_instant(meta.get("modifiedTime")),
            acls=owner_acl(self._context),
            raw_metadata={"kind": "drive_file", "source_mime": source_mime},
        )


class GoogleCalendarConnector(_GoogleConnector):
    """The `Connector` protocol against www.googleapis.com/calendar/v3, primary
    calendar only.

    A recurring event threads on its `recurringEventId`, so every instance of the
    weekly sync resolves to one thread the way a mail thread does.
    """

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        params: dict[str, Any] = {"maxResults": _PAGE_SIZE, "showDeleted": "false"}
        if since is not None:
            params["updatedMin"] = since.isoformat()
        async for events in self._pages(
            f"{_CALENDAR_API}/calendars/primary/events", params, items="items"
        ):
            for event in events:
                identifier = event.get("id")
                if not isinstance(identifier, str) or not identifier:
                    continue
                if event.get("status") == "cancelled":
                    continue
                yield f"event:{identifier}"

    async def fetch(self, external_id: str) -> RawDocument:
        """One event as a document.

        The body carries organizer and attendee display strings, which may include
        email addresses — that is content the caller could already read on the event,
        never a grant (ADR 0014).
        """
        if not external_id.startswith("event:"):
            raise ProviderApiError(
                "unrecognised google calendar external id shape", transient=False
            )
        event_id = external_id.removeprefix("event:")
        event = await self._http.get_json(f"{_CALENDAR_API}/calendars/primary/events/{event_id}")

        parts: list[str] = []
        description = event.get("description")
        if isinstance(description, str) and description.strip():
            parts.append(description.strip())
        organizer = event.get("organizer")
        if isinstance(organizer, dict):
            display = organizer.get("displayName") or organizer.get("email")
            if isinstance(display, str) and display:
                parts.append(f"Organizer: {display}")
        attendee_names: list[str] = []
        for attendee in event.get("attendees") or []:
            if not isinstance(attendee, dict):
                continue
            display = attendee.get("displayName") or attendee.get("email")
            if isinstance(display, str) and display:
                attendee_names.append(display)
        if attendee_names:
            parts.append("Attendees: " + ", ".join(attendee_names))

        thread_key = event.get("recurringEventId") or event.get("id") or event_id
        return RawDocument(
            external_id=external_id,
            source_system=self.system,
            uri=event.get("htmlLink"),
            title=str(event.get("summary") or "(untitled event)"),
            body="\n".join(parts),
            mime="text/plain",
            thread_id=f"gcal:{thread_key}",
            created_at=_instant(event.get("created")),
            modified_at=_instant(event.get("updated")),
            acls=owner_acl(self._context),
            raw_metadata={
                "kind": "calendar_event",
                "start": event.get("start"),
                "end": event.get("end"),
                "status": event.get("status"),
            },
        )


class GoogleMeetConnector(_GoogleConnector):
    """The `Connector` protocol against meet.googleapis.com/v2.

    The conference-records list API's `filter` syntax cannot express the start-time
    bound this walk needs, so the cursor is applied client-side: every record is
    listed and old ones are dropped here, at the cost of one `unchanged` outcome
    per re-listed record downstream.
    """

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        params: dict[str, Any] = {"pageSize": _MEET_PAGE_SIZE}
        async for records in self._pages(
            f"{_MEET_API}/conferenceRecords", params, items="conferenceRecords"
        ):
            for record in records:
                name = record.get("name")
                if not isinstance(name, str) or not name:
                    continue
                if since is not None and _instant(record.get("startTime")) < since:
                    continue
                yield f"conf:{name}"

    async def fetch(self, external_id: str) -> RawDocument:
        if not external_id.startswith("conf:"):
            raise ProviderApiError("unrecognised google meet external id shape", transient=False)
        name = external_id.removeprefix("conf:")
        if not name.startswith("conferenceRecords/"):
            raise ProviderApiError("unrecognised google meet external id shape", transient=False)
        record = await self._http.get_json(f"{_MEET_API}/{name}")
        start = record.get("startTime")
        end = record.get("endTime")
        space = record.get("space")
        meeting_code = space.get("meetingCode") if isinstance(space, dict) else None

        body = await self._transcript_body(name)
        if body is None:
            # A conference with no transcript still happened. Its times are real
            # provider metadata, not fake content — the one-line record below states
            # facts the API returned and invents nothing.
            body = (
                f"Conference {name} started {start or 'at an unknown time'} "
                f"and ended {end or 'at an unknown time'}; no transcript was recorded."
            )
            has_transcript = False
        else:
            has_transcript = True

        return RawDocument(
            external_id=external_id,
            source_system=self.system,
            title=f"Meet {meeting_code}" if isinstance(meeting_code, str) else name,
            body=body,
            mime="text/plain",
            thread_id=(
                f"meet:{meeting_code}" if isinstance(meeting_code, str) and meeting_code else None
            ),
            created_at=_instant(start),
            modified_at=_instant(end) if isinstance(end, str) and end else None,
            acls=owner_acl(self._context),
            raw_metadata={
                "kind": "meet_conference",
                "start_time": start,
                "end_time": end,
                "meeting_code": meeting_code,
                "has_transcript": has_transcript,
            },
        )

    async def _transcript_body(self, name: str) -> str | None:
        """Entries of the first ENDED transcript as "participant: text" lines, or None.

        Speakers are participant *resource names*, not display names — resolving them
        to people is entity work that happens downstream, over content, not here.
        """
        listing = await self._http.get_json(f"{_MEET_API}/{name}/transcripts")
        transcripts = listing.get("transcripts")
        if not isinstance(transcripts, list):
            return None
        ended = next(
            (
                transcript
                for transcript in transcripts
                if isinstance(transcript, dict) and transcript.get("state") == "ENDED"
            ),
            None,
        )
        if ended is None:
            return None
        transcript_name = ended.get("name")
        if not isinstance(transcript_name, str) or not transcript_name:
            return None

        lines: list[str] = []
        async for entries in self._pages(
            f"{_MEET_API}/{transcript_name}/entries",
            {"pageSize": _MEET_PAGE_SIZE},
            items="transcriptEntries",
        ):
            for entry in entries:
                text = entry.get("text")
                if not isinstance(text, str) or not text:
                    continue
                participant = entry.get("participant")
                speaker = participant if isinstance(participant, str) and participant else "unknown"
                lines.append(f"{speaker}: {text}")
        return "\n".join(lines) if lines else None
