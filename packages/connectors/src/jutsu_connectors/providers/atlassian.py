"""Jira and Confluence over the Atlassian cloud gateway (api.atlassian.com).

Both products sit behind one OAuth app and one gateway, so they share site
resolution: `accessible-resources` names the sites the token reaches and the first
one is the site this connection syncs. A token that reaches several sites syncs only
that first site — more sites mean more connections, and this module does not pretend
otherwise.

One document shape each, with stable external ids:

    issue:{KEY}   one Jira issue, e.g. issue:ENG-7
    page:{id}     one Confluence page, e.g. page:98305

Issues thread by project and pages by space — the containers cross-references
actually resolve within. Jira issue security schemes and Confluence space
permissions are provider-side sharing this module cannot yet express as *subjects*,
so every document is granted to the connecting user alone (ADR 0014).
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlparse

import httpx
from jutsu_core.models import AclEntry, RawDocument, SourceSystem

from jutsu_connectors.providers.base import (
    ListingIncomplete,
    ProviderApiError,
    ProviderContext,
    ProviderHttp,
    TokenSource,
    owner_acl,
    parse_cursor,
)

_API = "https://api.atlassian.com"
_PAGE_SIZE = 50
#: Bounded pagination: a runaway listing is a provider bug amplified into a stuck
#: walk. 50 pages of 50 covers any tenant a single connection should drain per sync.
_MAX_PAGES = 50

#: JQL takes minute precision and no zone designator; CQL the same with slashed
#: dates. The instant is rendered in UTC — any skew against the site's zone only
#: re-lists documents, and content hashing downstream deduplicates them.
_JQL_MINUTE = "%Y-%m-%d %H:%M"
_CQL_MINUTE = "%Y/%m/%d %H:%M"

_ISSUE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[0-9]+")

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _instant(value: Any) -> datetime:
    """Atlassian timestamps: Jira offsets like `+0000`, Confluence a `Z` suffix.
    Both are ISO-8601 that Python 3.12's `fromisoformat` accepts directly."""
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(tz=UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(tz=UTC)


def _adf_text(node: Any) -> str:
    """Flatten Atlassian Document Format to plain text.

    ADF is a tree of `{"type", "content", "text"}` nodes. Runs of text nodes inside
    one block join seamlessly (marks split a sentence into several nodes), block
    nodes land on their own lines, and anything unrecognised — including a missing
    description, which Jira sends as null — flattens to the empty string.
    """
    if isinstance(node, list):
        rendered = (_adf_text(child) for child in node)
        return "\n".join(part for part in rendered if part)
    if not isinstance(node, dict):
        return ""
    text = node.get("text")
    if isinstance(text, str):
        return text
    content = node.get("content")
    if not isinstance(content, list):
        return ""
    if content and all(
        isinstance(child, dict) and isinstance(child.get("text"), str) for child in content
    ):
        return "".join(_adf_text(child) for child in content)
    return _adf_text(content)


def _html_text(markup: Any) -> str:
    """Flatten Confluence storage-format XHTML to plain text.

    Tags strip before entities unescape, so `&lt;b&gt;` in a page stays the literal
    text the author wrote instead of becoming a tag this function then eats.
    """
    if not isinstance(markup, str) or not markup:
        return ""
    stripped = _TAG.sub(" ", markup)
    return _WHITESPACE.sub(" ", html.unescape(stripped)).strip()


class _AtlassianConnector:
    """Shared gateway plumbing: one resolved cloud id per connector lifetime."""

    system: SourceSystem

    def __init__(
        self, context: ProviderContext, token: TokenSource, client: httpx.AsyncClient
    ) -> None:
        self._context = context
        self._http = ProviderHttp(token, client)
        self._cloud_id: str | None = None

    async def _cloud(self) -> str:
        """The first site `accessible-resources` names, cached for the connector's
        lifetime — a site does not move mid-sync, and every listed or fetched
        document must come from the same one."""
        if self._cloud_id is not None:
            return self._cloud_id
        # get_json requires an object; this one endpoint answers a JSON list.
        response = await self._http.request("GET", f"{_API}/oauth/token/accessible-resources")
        payload = response.json()
        if not isinstance(payload, list):
            raise ProviderApiError("accessible-resources did not answer a list", transient=False)
        if not payload:
            raise ProviderApiError("the token reaches no Atlassian site", transient=False)
        first = payload[0]
        cloud_id = first.get("id") if isinstance(first, dict) else None
        if not isinstance(cloud_id, str) or not cloud_id:
            raise ProviderApiError(
                "accessible-resources named a site without an id", transient=False
            )
        self._cloud_id = cloud_id
        return cloud_id

    async def aclose(self) -> None:
        """Release the HTTP client.

        `close_connector` looks this method up with `getattr` and silently does
        nothing when it is absent — so a connector without one leaked its httpx
        client, and with it a connection pool, once per job. Two of the nine
        defined it; the rest inherited a no-op that read like cleanup.
        """
        await self._http.aclose()

    async def acls(self, external_id: str) -> list[AclEntry]:
        return owner_acl(self._context)


class JiraConnector(_AtlassianConnector):
    """The `Connector` protocol against Jira Cloud's REST API v3."""

    system = SourceSystem.JIRA

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        cloud = await self._cloud()
        if since is None:
            jql = "order by updated asc"
        else:
            stamp = since.astimezone(UTC).strftime(_JQL_MINUTE)
            jql = f'updated >= "{stamp}" order by updated asc'
        # **`/rest/api/3/search` is gone, and `/rest/api/3/search/jql` replaced it.**
        # Atlassian removed the offset-paged endpoint along with the rest of the
        # `startAt`/`total` search family; calling it now answers 410, which
        # `ProviderHttp` classifies as a permanent rejection — so the connector listed
        # nothing, for ever, and every Jira sync failed on its first call. The
        # replacement is token-paged and deliberately returns no `total`: there is no
        # count to compare against, and `isLast`/`nextPageToken` is the whole
        # termination signal.
        listed = 0
        next_token: str | None = None
        for _page in range(_MAX_PAGES):
            params: dict[str, Any] = {
                "jql": jql,
                "fields": "updated",
                "maxResults": _PAGE_SIZE,
            }
            if next_token:
                params["nextPageToken"] = next_token
            payload = await self._http.get_json(
                f"{_API}/ex/jira/{cloud}/rest/api/3/search/jql", params=params
            )
            issues = payload.get("issues")
            if not isinstance(issues, list):
                raise ProviderApiError("Jira answered a search without issues", transient=False)
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                key = issue.get("key")
                if not isinstance(key, str) or not key:
                    continue
                if since is not None:
                    # JQL's minute precision re-lists the tail of the cursor's
                    # minute; `updated` is requested so this drops it exactly.
                    updated = _instant((issue.get("fields") or {}).get("updated"))
                    if updated < since:
                        continue
                listed += 1
                yield f"issue:{key}"
            raw_token = payload.get("nextPageToken")
            next_token = raw_token if isinstance(raw_token, str) and raw_token else None
            # `isLast` is authoritative when Jira sends it; a missing token means the
            # same thing and is what older responses use to say it.
            if payload.get("isLast") is True or next_token is None:
                return
        raise ListingIncomplete(listed, pages=_MAX_PAGES)

    async def fetch(self, external_id: str) -> RawDocument:
        if external_id.startswith("issue:"):
            key = external_id.removeprefix("issue:")
            if _ISSUE_KEY.fullmatch(key):
                return await self._fetch_issue(key)
        raise ProviderApiError("unrecognised jira external id shape", transient=False)

    async def _fetch_issue(self, key: str) -> RawDocument:
        cloud = await self._cloud()
        payload = await self._http.get_json(
            f"{_API}/ex/jira/{cloud}/rest/api/3/issue/{key}",
            params={
                "fields": "summary,description,created,updated,reporter,assignee,status,project"
            },
        )
        fields = payload.get("fields") or {}
        summary = str(fields.get("summary") or key)
        description = _adf_text(fields.get("description"))
        reporter = fields.get("reporter") or {}
        account_id = reporter.get("accountId")
        project_key = (fields.get("project") or {}).get("key")
        status = fields.get("status") or {}
        assignee = fields.get("assignee") or {}
        return RawDocument(
            external_id=f"issue:{key}",
            source_system=self.system,
            title=f"{key}: {summary}",
            body=f"{summary}\n\n{description}" if description else summary,
            author_external_id=(
                f"{self.system.value}:{account_id}"
                if isinstance(account_id, str) and account_id
                else None
            ),
            thread_id=(
                f"{self.system.value}:{project_key}"
                if isinstance(project_key, str) and project_key
                else None
            ),
            created_at=_instant(fields.get("created")),
            modified_at=_instant(fields.get("updated")),
            acls=owner_acl(self._context),
            raw_metadata={
                "kind": "issue",
                "status": status.get("name"),
                "assignee": assignee.get("accountId"),
            },
        )


def _next_params(link: str) -> dict[str, Any]:
    """The query of a Confluence `_links.next`, as parameters.

    The value is a relative reference like
    `/wiki/rest/api/content/search?cql=...&cursor=...&limit=25`. Only its query is
    used: the path is rebuilt from the cloud id this connector already resolved, so a
    link that pointed somewhere else could not move the request.
    """
    query = urlparse(link).query
    return {key: value for key, value in parse_qsl(query, keep_blank_values=False)}


class ConfluenceConnector(_AtlassianConnector):
    """The `Connector` protocol against Confluence Cloud's content REST API."""

    system = SourceSystem.CONFLUENCE

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        cloud = await self._cloud()
        if since is None:
            cql = "type=page order by lastmodified asc"
        else:
            stamp = since.astimezone(UTC).strftime(_CQL_MINUTE)
            cql = f'type=page and lastmodified >= "{stamp}" order by lastmodified asc'
        # **Follow the link Confluence hands back rather than counting offsets.**
        # `content/search` is cursor-paged: `_links.next` carries a `cursor`
        # parameter that encodes the server's position, and a re-issued `start`
        # offset over a CQL search is not guaranteed to line up with it — Confluence
        # is explicit that deep offsets over search are unstable, so a walk that
        # recomputed `start` could repeat a page or step over one. The offset is
        # kept only as the opening position.
        start = 0
        next_query: str | None = None
        for _page in range(_MAX_PAGES):
            params: dict[str, Any] = (
                _next_params(next_query)
                if next_query
                else {"cql": cql, "limit": _PAGE_SIZE, "start": start}
            )
            payload = await self._http.get_json(
                f"{_API}/ex/confluence/{cloud}/wiki/rest/api/content/search",
                params=params,
            )
            results = payload.get("results")
            if not isinstance(results, list):
                raise ProviderApiError(
                    "Confluence answered a search without results", transient=False
                )
            if not results:
                return
            for item in results:
                if not isinstance(item, dict):
                    continue
                page_id = item.get("id")
                if isinstance(page_id, str) and page_id:
                    yield f"page:{page_id}"
            # Confluence caps the requested limit server-side, so termination
            # compares the answer's own size and limit, never the request's.
            size = payload.get("size")
            limit = payload.get("limit")
            links = payload.get("_links")
            raw_next = links.get("next") if isinstance(links, dict) else None
            if not isinstance(size, int) or not isinstance(limit, int):
                return
            if size < limit or not isinstance(raw_next, str) or not raw_next:
                return
            next_query = raw_next
            start += size
        raise ListingIncomplete(start, pages=_MAX_PAGES)

    async def fetch(self, external_id: str) -> RawDocument:
        if external_id.startswith("page:"):
            page_id = external_id.removeprefix("page:")
            if page_id.isdigit():
                return await self._fetch_page(page_id)
        raise ProviderApiError("unrecognised confluence external id shape", transient=False)

    async def _fetch_page(self, page_id: str) -> RawDocument:
        cloud = await self._cloud()
        payload = await self._http.get_json(
            f"{_API}/ex/confluence/{cloud}/wiki/rest/api/content/{page_id}",
            params={"expand": "body.storage,version,history,space"},
        )
        title = str(payload.get("title") or f"page {page_id}")
        storage = (payload.get("body") or {}).get("storage") or {}
        body = _html_text(storage.get("value"))
        history = payload.get("history") or {}
        account_id = (history.get("createdBy") or {}).get("accountId")
        version = payload.get("version") or {}
        space_key = (payload.get("space") or {}).get("key")
        links = payload.get("_links") or {}
        base = links.get("base")
        webui = links.get("webui")
        return RawDocument(
            external_id=f"page:{page_id}",
            source_system=self.system,
            uri=(
                f"{base}{webui}"
                if isinstance(base, str) and base and isinstance(webui, str) and webui
                else None
            ),
            title=title,
            body=body if body else title,
            author_external_id=(
                f"{self.system.value}:{account_id}"
                if isinstance(account_id, str) and account_id
                else None
            ),
            thread_id=(
                f"{self.system.value}:{space_key}"
                if isinstance(space_key, str) and space_key
                else None
            ),
            created_at=_instant(history.get("createdDate")),
            modified_at=_instant(version.get("when")),
            acls=owner_acl(self._context),
            raw_metadata={
                "kind": "page",
                "space": space_key,
                "version": version.get("number"),
            },
        )
