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
        start_at = 0
        for _page in range(_MAX_PAGES):
            payload = await self._http.get_json(
                f"{_API}/ex/jira/{cloud}/rest/api/3/search",
                params={
                    "jql": jql,
                    "fields": "updated",
                    "maxResults": _PAGE_SIZE,
                    "startAt": start_at,
                },
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
                yield f"issue:{key}"
            total = payload.get("total")
            start_at += len(issues)
            if not issues or not isinstance(total, int) or start_at >= total:
                return

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
        start = 0
        for _page in range(_MAX_PAGES):
            payload = await self._http.get_json(
                f"{_API}/ex/confluence/{cloud}/wiki/rest/api/content/search",
                params={"cql": cql, "limit": _PAGE_SIZE, "start": start},
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
            has_next = isinstance(links, dict) and "next" in links
            if not isinstance(size, int) or not isinstance(limit, int):
                return
            if size < limit or not has_next:
                return
            start += size

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
