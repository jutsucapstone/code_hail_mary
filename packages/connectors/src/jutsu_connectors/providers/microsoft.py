"""Microsoft 365 over Graph v1.0: OneDrive files, Teams chat messages and SharePoint
site documents. Delegated read scopes only (Files.Read, Chat.Read, Sites.Read.All) —
no write scope, ever (§4.8). One Entra directory object id spans all three surfaces,
so every principal and author this module mints is `m365:{object id}` (ADR 0014).

Document shapes and their stable external ids:

    item:{itemId}              one OneDrive file
    chat:{chatId}:{messageId}  one Teams chat message (the chat is the thread)
    site:{siteId}:{itemId}     one SharePoint document-library file (the site is the thread)

Honest bounds, so nobody discovers them in production:

* Drive delta tokens are deliberately not persisted — the walk's cursor is an instant,
  so every walk re-enumerates the full delta and filters `lastModifiedDateTime`
  client-side, and the repeated listings deduplicate downstream on content hash.
* SharePoint reads at most the first ten sites of `/sites?search=*`; a larger estate
  needs a site-catalogue slice this module does not pretend to be.
* A site without a document library answers 404 on its drive delta. That is an empty
  site, not a failure, and the walk continues past it.
* Only text-ish files under 1MB sync (`text/*` mime, or a .md/.txt/.csv/.json/.rst
  name): the pipeline chunks text, and a binary shipped through it is quota spent on
  noise.
"""

from __future__ import annotations

import html
import re
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

_GRAPH = "https://graph.microsoft.com/v1.0"
_PAGE_SIZE = 50
#: Bounded pagination: a runaway `@odata.nextLink` chain is a provider bug amplified
#: into a stuck walk. Fifty pages per listing is beyond any single user's surface and
#: still finite.
_MAX_PAGES = 50
#: The SharePoint bound: the first page of `/sites?search=*`, ten sites, no further.
_MAX_SITES = 10
_MAX_FILE_BYTES = 1_000_000
_TEXT_SUFFIXES = (".md", ".txt", ".csv", ".json", ".rst")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_LINE_BREAKS = re.compile(r"<br\s*/?>|</p>|</div>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")


def _html_text(markup: str) -> str:
    """Graph chat bodies arrive as HTML; the pipeline stores prose.

    Block closers become newlines, every other tag disappears, entities decode. A chat
    message is a paragraph, not a document — anything smarter is an HTML parser this
    flattening does not need.
    """
    text = _LINE_BREAKS.sub("\n", markup)
    text = _TAGS.sub("", text)
    return html.unescape(text).strip()


def _instant(value: Any) -> datetime:
    """Graph timestamps are ISO-8601 with a Z suffix, always UTC."""
    if not isinstance(value, str) or not value:
        return datetime.now(tz=UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(tz=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _user_id(identity_set: Any) -> str | None:
    """The directory object id inside a Graph identitySet, when one is present.

    Informational only — authorship is never a grant (ADR 0014).
    """
    if not isinstance(identity_set, dict):
        return None
    user = identity_set.get("user")
    if not isinstance(user, dict):
        return None
    user_id = user.get("id")
    return user_id if isinstance(user_id, str) and user_id else None


def _values(page: dict[str, Any]) -> list[dict[str, Any]]:
    raw = page.get("value")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _syncable_file(item: dict[str, Any]) -> bool:
    """Folders, packages and deleted delta entries carry no text; binaries and
    anything at or over 1MB are excluded because the pipeline chunks prose, not
    attachments."""
    if "deleted" in item:
        return False
    file_facet = item.get("file")
    if not isinstance(file_facet, dict):
        return False
    size = item.get("size")
    if not isinstance(size, int) or size >= _MAX_FILE_BYTES:
        return False
    mime = file_facet.get("mimeType")
    if isinstance(mime, str) and mime.startswith("text/"):
        return True
    name = item.get("name")
    return isinstance(name, str) and name.lower().endswith(_TEXT_SUFFIXES)


def _syncable_message(message: dict[str, Any], since: datetime | None) -> bool:
    """Only human messages carry prose: system events (member joins, renames) and
    empty bodies are noise the chunker would dutifully embed."""
    if message.get("messageType") != "message":
        return False
    body = message.get("body")
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, str) or not content.strip():
        return False
    if since is not None:
        stamp = message.get("lastModifiedDateTime") or message.get("createdDateTime")
        if _instant(stamp) < since:
            return False
    return True


async def _odata_pages(
    http: ProviderHttp, url: str, params: dict[str, Any] | None = None
) -> AsyncIterator[dict[str, Any]]:
    """Follow `@odata.nextLink` verbatim — it is an absolute URL carrying its own
    query — and stop when it is absent (a delta listing ends on `@odata.deltaLink`,
    which is deliberately not kept: the walk's cursor is an instant, not a token)."""
    next_url = url
    next_params = params
    for _ in range(_MAX_PAGES):
        payload = await http.get_json(next_url, params=next_params)
        yield payload
        next_link = payload.get("@odata.nextLink")
        if not isinstance(next_link, str) or not next_link:
            return
        next_url = next_link
        next_params = None


async def _download_text(http: ProviderHttp, client: httpx.AsyncClient, url: str) -> str:
    """Drive content answers 302 to a pre-authenticated storage URL.

    The redirect is followed with a plain unauthenticated GET on purpose: the
    destination is not graph.microsoft.com, and a Bearer header sent there hands the
    token to a storage host that never asked for it. This is the one sanctioned call
    in this module that bypasses `ProviderHttp`.
    """
    response = await http.request("GET", url)
    if response.status_code in _REDIRECT_STATUSES:
        location = response.headers.get("Location")
        if not location:
            raise ProviderApiError(
                "the provider redirected a download without a destination", transient=False
            )
        try:
            response = await client.get(location)
        except httpx.TimeoutException as error:
            raise ProviderApiError("the content download timed out", transient=True) from error
        except httpx.HTTPError as error:
            raise ProviderApiError(
                "the content download failed to complete", transient=True
            ) from error
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderApiError("the download host failed the request", transient=True)
        if response.status_code >= 400:
            raise ProviderApiError(
                f"the download host rejected the request (HTTP {response.status_code})",
                transient=False,
            )
    return response.text


async def _drive_document(
    http: ProviderHttp,
    client: httpx.AsyncClient,
    context: ProviderContext,
    *,
    item_url: str,
    external_id: str,
    thread_id: str | None,
    raw_metadata: dict[str, Any],
) -> RawDocument:
    """OneDrive and SharePoint items share one driveItem shape and one download dance."""
    item = await http.get_json(item_url)
    body = await _download_text(http, client, f"{item_url}/content")
    file_facet = item.get("file")
    mime = file_facet.get("mimeType") if isinstance(file_facet, dict) else None
    author_id = _user_id(item.get("createdBy"))
    name = item.get("name")
    return RawDocument(
        external_id=external_id,
        source_system=SourceSystem.M365,
        uri=item.get("webUrl"),
        title=name if isinstance(name, str) and name else external_id,
        body=body,
        mime=mime if isinstance(mime, str) and mime else "text/plain",
        author_external_id=(
            f"{SourceSystem.M365.value}:{author_id}" if author_id is not None else None
        ),
        thread_id=thread_id,
        created_at=_instant(item.get("createdDateTime")),
        modified_at=_instant(item.get("lastModifiedDateTime")),
        acls=owner_acl(context),
        raw_metadata=raw_metadata,
    )


class OneDriveConnector:
    """The `Connector` protocol over the connecting user's own OneDrive."""

    system = SourceSystem.M365

    def __init__(
        self, context: ProviderContext, token: TokenSource, client: httpx.AsyncClient
    ) -> None:
        self._context = context
        self._http = ProviderHttp(token, client)
        self._client = client

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        async for page in _odata_pages(self._http, f"{_GRAPH}/me/drive/root/delta"):
            for item in _values(page):
                if not _syncable_file(item):
                    continue
                if since is not None and _instant(item.get("lastModifiedDateTime")) < since:
                    continue
                item_id = item.get("id")
                if isinstance(item_id, str) and item_id:
                    yield f"item:{item_id}"

    async def fetch(self, external_id: str) -> RawDocument:
        if external_id.startswith("item:"):
            item_id = external_id.removeprefix("item:")
            if item_id:
                return await _drive_document(
                    self._http,
                    self._client,
                    self._context,
                    item_url=f"{_GRAPH}/me/drive/items/{item_id}",
                    external_id=external_id,
                    thread_id=None,
                    raw_metadata={"kind": "onedrive_file"},
                )
        raise ProviderApiError("unrecognised onedrive external id shape", transient=False)

    async def acls(self, external_id: str) -> list[AclEntry]:
        return owner_acl(self._context)


class TeamsConnector:
    """The `Connector` protocol over the connecting user's Teams chats."""

    system = SourceSystem.M365

    def __init__(
        self, context: ProviderContext, token: TokenSource, client: httpx.AsyncClient
    ) -> None:
        self._context = context
        self._http = ProviderHttp(token, client)
        # One topic lookup per chat per walk: refetching it per message would multiply
        # the request count by the chat's length for a string that names the thread.
        self._topics: dict[str, str | None] = {}

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        async for chats in _odata_pages(
            self._http, f"{_GRAPH}/me/chats", params={"$top": _PAGE_SIZE}
        ):
            for chat in _values(chats):
                chat_id = chat.get("id")
                if not isinstance(chat_id, str) or not chat_id:
                    continue
                async for messages in _odata_pages(
                    self._http,
                    f"{_GRAPH}/me/chats/{chat_id}/messages",
                    params={"$top": _PAGE_SIZE},
                ):
                    for message in _values(messages):
                        if not _syncable_message(message, since):
                            continue
                        message_id = message.get("id")
                        if isinstance(message_id, str) and message_id:
                            yield f"chat:{chat_id}:{message_id}"

    async def fetch(self, external_id: str) -> RawDocument:
        # Chat ids carry colons ("19:...@thread.v2"); message ids never do, so the
        # last colon is the only unambiguous split point.
        if external_id.startswith("chat:"):
            chat_id, _, message_id = external_id.removeprefix("chat:").rpartition(":")
            if chat_id and message_id:
                return await self._fetch_message(chat_id, message_id)
        raise ProviderApiError("unrecognised teams external id shape", transient=False)

    async def _fetch_message(self, chat_id: str, message_id: str) -> RawDocument:
        message = await self._http.get_json(f"{_GRAPH}/me/chats/{chat_id}/messages/{message_id}")
        body = message.get("body")
        content = body.get("content") if isinstance(body, dict) else None
        topic = await self._chat_topic(chat_id)
        author_id = _user_id(message.get("from"))
        return RawDocument(
            external_id=f"chat:{chat_id}:{message_id}",
            source_system=self.system,
            uri=message.get("webUrl"),
            title=topic or "Chat message",
            body=_html_text(content) if isinstance(content, str) else "",
            mime="text/plain",
            author_external_id=(
                f"{self.system.value}:{author_id}" if author_id is not None else None
            ),
            thread_id=f"m365-chat:{chat_id}",
            created_at=_instant(message.get("createdDateTime")),
            modified_at=_instant(
                message.get("lastModifiedDateTime") or message.get("createdDateTime")
            ),
            acls=owner_acl(self._context),
            raw_metadata={
                "kind": "chat_message",
                "chat_id": chat_id,
                "message_type": message.get("messageType"),
            },
        )

    async def _chat_topic(self, chat_id: str) -> str | None:
        if chat_id not in self._topics:
            chat = await self._http.get_json(f"{_GRAPH}/me/chats/{chat_id}")
            topic = chat.get("topic")
            self._topics[chat_id] = topic if isinstance(topic, str) and topic else None
        return self._topics[chat_id]

    async def acls(self, external_id: str) -> list[AclEntry]:
        return owner_acl(self._context)


class SharePointConnector:
    """The `Connector` protocol over SharePoint site document libraries.

    Bounded to the first ten sites the search returns. A site whose drive answers a
    permanent error has no document library — a brand-new team site legitimately has
    none — and is treated as empty rather than failing the walk; a dead token is not
    that, and still fails it.
    """

    system = SourceSystem.M365

    def __init__(
        self, context: ProviderContext, token: TokenSource, client: httpx.AsyncClient
    ) -> None:
        self._context = context
        self._http = ProviderHttp(token, client)
        self._client = client

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        catalogue = await self._http.get_json(f"{_GRAPH}/sites", params={"search": "*"})
        for site in _values(catalogue)[:_MAX_SITES]:
            site_id = site.get("id")
            if not isinstance(site_id, str) or not site_id:
                continue
            try:
                async for page in _odata_pages(
                    self._http, f"{_GRAPH}/sites/{site_id}/drive/root/delta"
                ):
                    for item in _values(page):
                        if not _syncable_file(item):
                            continue
                        if since is not None and _instant(item.get("lastModifiedDateTime")) < since:
                            continue
                        item_id = item.get("id")
                        if isinstance(item_id, str) and item_id:
                            yield f"site:{site_id}:{item_id}"
            except ProviderApiError as error:
                if error.transient or isinstance(error, ProviderAuthError):
                    raise
                continue

    async def fetch(self, external_id: str) -> RawDocument:
        if external_id.startswith("site:"):
            site_id, _, item_id = external_id.removeprefix("site:").partition(":")
            if site_id and item_id:
                return await _drive_document(
                    self._http,
                    self._client,
                    self._context,
                    item_url=f"{_GRAPH}/sites/{site_id}/drive/items/{item_id}",
                    external_id=external_id,
                    thread_id=f"m365-site:{site_id}",
                    raw_metadata={"kind": "sharepoint_file", "site_id": site_id},
                )
        raise ProviderApiError("unrecognised sharepoint external id shape", transient=False)

    async def acls(self, external_id: str) -> list[AclEntry]:
        return owner_acl(self._context)
