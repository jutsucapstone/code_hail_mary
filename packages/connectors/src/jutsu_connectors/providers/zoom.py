"""Zoom over the REST API: cloud recordings, preferring their transcripts.

What a meeting *said* lives in the transcript Zoom generates for cloud recordings, so
that is the document: one per recording instance, bodied by the VTT transcript when
one exists and by honest metadata (topic, when, how long) when it does not. Live
meeting audio, chat and dashboards are out of scope — they either need write-adjacent
scopes or describe presence rather than knowledge.

    recording:{meeting_uuid}    one cloud-recorded meeting instance

Two API quirks are load-bearing here:

* **The listing window is capped at one month.** `GET /users/me/recordings` rejects a
  `from`/`to` range wider than 30 days, so a first sync walks backwards-bounded
  windows (180 days of history) and an incremental sync windows forward from the
  cursor. Adjacent windows share a boundary day deliberately — an overlap costs one
  `unchanged` outcome downstream, a gap loses a recording for ever.
* **Meeting UUIDs must be double-encoded when they start with `/` or contain `//`.**
  Zoom documents this; a single encoding 404s exactly those meetings and nothing
  else, which reads as flakiness rather than a bug.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

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

_API = "https://api.zoom.us/v2"
_PAGE_SIZE = 300
#: Bounded pagination per window, same rationale as GitHub's: a runaway listing is a
#: provider bug amplified into a stuck walk.
_MAX_PAGES = 50
#: Zoom rejects a from/to range wider than a month.
_WINDOW_DAYS = 30
#: How far a FIRST sync reaches back. Recordings older than this arrive when Zoom
#: history import becomes its own slice; an unbounded walk would spend the rate
#: limit re-reading years of silence on every reconnect.
_LOOKBACK_DAYS = 180


def _instant(value: Any) -> datetime:
    """Zoom timestamps are ISO-8601 with a Z suffix, always UTC."""
    if not isinstance(value, str) or not value:
        return datetime.now(tz=UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(tz=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _encoded_uuid(value: str) -> str:
    """Path-encode a meeting UUID, doubly where Zoom demands it (see module doc)."""
    quoted = quote(value, safe="")
    if value.startswith("/") or "//" in value:
        quoted = quote(quoted, safe="")
    return quoted


def _vtt_to_text(vtt: str) -> str:
    """A WebVTT transcript as plain prose: cue text (with speaker labels) only.

    Timing lines, cue numbers and the header carry no knowledge; dropping them is
    what makes a transcript chunk like a document instead of like a log file.
    """
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            continue
        lines.append(line)
    return "\n".join(lines)


class ZoomConnector:
    """The `Connector` protocol against api.zoom.us."""

    system = SourceSystem.ZOOM

    def __init__(
        self, context: ProviderContext, token: TokenSource, client: httpx.AsyncClient
    ) -> None:
        self._context = context
        self._http = ProviderHttp(token, client)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        now = datetime.now(tz=UTC)
        window_start = since if since is not None else now - timedelta(days=_LOOKBACK_DAYS)
        while True:
            window_end = min(window_start + timedelta(days=_WINDOW_DAYS), now)
            next_token = ""
            for _page in range(_MAX_PAGES):
                params: dict[str, Any] = {
                    "page_size": _PAGE_SIZE,
                    "from": window_start.date().isoformat(),
                    "to": window_end.date().isoformat(),
                }
                if next_token:
                    params["next_page_token"] = next_token
                payload = await self._http.get_json(f"{_API}/users/me/recordings", params=params)
                for meeting in payload.get("meetings") or []:
                    if not isinstance(meeting, dict):
                        continue
                    meeting_uuid = meeting.get("uuid")
                    if isinstance(meeting_uuid, str) and meeting_uuid:
                        yield f"recording:{meeting_uuid}"
                next_token = str(payload.get("next_page_token") or "")
                if not next_token:
                    break
            if window_end >= now:
                return
            # The next window REUSES the boundary day: from/to are inclusive dates,
            # and re-listing one day beats losing a recording in the seam.
            window_start = window_end

    async def fetch(self, external_id: str) -> RawDocument:
        if not external_id.startswith("recording:"):
            raise ProviderApiError("unrecognised zoom external id shape", transient=False)
        meeting_uuid = external_id.removeprefix("recording:")
        payload = await self._http.get_json(
            f"{_API}/meetings/{_encoded_uuid(meeting_uuid)}/recordings"
        )
        topic = str(payload.get("topic") or "Zoom meeting")
        started = _instant(payload.get("start_time"))
        host_id = payload.get("host_id")
        duration = payload.get("duration")

        body = ""
        transcript_file = next(
            (
                item
                for item in payload.get("recording_files") or []
                if isinstance(item, dict)
                and item.get("file_type") == "TRANSCRIPT"
                and isinstance(item.get("download_url"), str)
            ),
            None,
        )
        if transcript_file is not None:
            body = _vtt_to_text(await self._http.get_text(str(transcript_file["download_url"])))
        if not body:
            # No transcript (audio-only plan, processing still running): the honest
            # document is the meeting's own facts, never a synthesised summary.
            when = started.strftime("%Y-%m-%d %H:%M UTC")
            length = f" lasting {int(duration)} minutes" if isinstance(duration, int) else ""
            body = f"{topic}\n\nCloud-recorded Zoom meeting on {when}{length}. No transcript was available for this recording."

        return RawDocument(
            external_id=external_id,
            source_system=self.system,
            uri=payload.get("share_url"),
            title=topic,
            body=body,
            mime="text/plain",
            author_external_id=(
                f"{self.system.value}:{host_id}" if isinstance(host_id, str) and host_id else None
            ),
            # The numeric meeting id survives across a recurring meeting's instances;
            # its recordings thread the way a mail thread does.
            thread_id=(f"zoom:{payload['id']}" if payload.get("id") is not None else None),
            created_at=started,
            modified_at=started,
            acls=owner_acl(self._context),
            raw_metadata={
                "kind": "recording",
                "duration_minutes": duration if isinstance(duration, int) else None,
                "has_transcript": transcript_file is not None,
            },
        )

    async def acls(self, external_id: str) -> list[AclEntry]:
        return owner_acl(self._context)
