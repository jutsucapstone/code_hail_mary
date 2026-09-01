"""GitHub over the REST API: READMEs and issues from repositories the token can see.

Scopes are `read:user` + `read:org` only — the classic `repo` scope also writes, so it
is refused (§4.8), which bounds this connector to repositories visible without it:
public repositories the account owns, collaborates on, or reaches through org
membership. Private-repo content is a GitHub App concern (ADR 0014 records why), and
this module does not pretend otherwise.

Two document shapes per repository, each with a stable external id:

    readme:{owner}/{repo}          the repository README, raw
    issue:{owner}/{repo}#{number}  one issue (or pull request — GitHub lists both)

Issues thread naturally: the repository is the thread, so cross-references inside one
repo resolve during entity work the way a mail thread does.
"""

from __future__ import annotations

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

_API = "https://api.github.com"
_PER_PAGE = 100
#: Bounded pagination: a runaway listing is a provider bug amplified into a stuck
#: walk. 50 pages of 100 is far beyond any personal account and still finite.
_MAX_PAGES = 50


def _instant(value: Any) -> datetime:
    """GitHub timestamps are ISO-8601 with a Z suffix, always UTC."""
    if not isinstance(value, str) or not value:
        return datetime.now(tz=UTC)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class GitHubConnector:
    """The `Connector` protocol against api.github.com."""

    system = SourceSystem.GITHUB

    def __init__(
        self, context: ProviderContext, token: TokenSource, client: httpx.AsyncClient
    ) -> None:
        self._context = context
        self._http = ProviderHttp(token, client)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _pages(self, url: str, params: dict[str, Any]) -> AsyncIterator[list[Any]]:
        for page in range(1, _MAX_PAGES + 1):
            response = await self._http.request(
                "GET", url, params={**params, "per_page": _PER_PAGE, "page": page}
            )
            payload = response.json()
            if not isinstance(payload, list):
                raise ProviderApiError(
                    "GitHub answered a list call without a list", transient=False
                )
            if payload:
                yield payload
            if len(payload) < _PER_PAGE:
                return

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        since = parse_cursor(cursor)
        async for repos in self._pages(
            f"{_API}/user/repos", {"sort": "pushed", "direction": "desc"}
        ):
            for repo in repos:
                full_name = repo.get("full_name")
                if not isinstance(full_name, str) or not full_name:
                    continue
                pushed_at = _instant(repo.get("pushed_at"))
                if since is not None and pushed_at < since:
                    # Sorted by pushed descending: everything after this is older.
                    return
                yield f"readme:{full_name}"
                params: dict[str, Any] = {"state": "all", "sort": "updated", "direction": "asc"}
                if since is not None:
                    params["since"] = since.isoformat()
                async for issues in self._pages(f"{_API}/repos/{full_name}/issues", params):
                    for issue in issues:
                        number = issue.get("number")
                        if isinstance(number, int):
                            yield f"issue:{full_name}#{number}"

    async def fetch(self, external_id: str) -> RawDocument:
        if external_id.startswith("readme:"):
            return await self._fetch_readme(external_id.removeprefix("readme:"))
        if external_id.startswith("issue:"):
            reference = external_id.removeprefix("issue:")
            full_name, _, number = reference.partition("#")
            if full_name and number.isdigit():
                return await self._fetch_issue(full_name, int(number))
        raise ProviderApiError("unrecognised github external id shape", transient=False)

    async def _fetch_readme(self, full_name: str) -> RawDocument:
        repo = await self._http.get_json(f"{_API}/repos/{full_name}")
        body = await self._http.get_text(
            f"{_API}/repos/{full_name}/readme",
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        return RawDocument(
            external_id=f"readme:{full_name}",
            source_system=self.system,
            uri=repo.get("html_url"),
            title=f"{full_name} — README",
            body=body,
            mime="text/markdown",
            thread_id=f"github:{full_name}",
            created_at=_instant(repo.get("created_at")),
            modified_at=_instant(repo.get("pushed_at")),
            acls=owner_acl(self._context),
            raw_metadata={"repo": full_name, "kind": "readme"},
        )

    async def _fetch_issue(self, full_name: str, number: int) -> RawDocument:
        issue = await self._http.get_json(f"{_API}/repos/{full_name}/issues/{number}")
        title = str(issue.get("title") or f"{full_name}#{number}")
        body = str(issue.get("body") or "")
        author = issue.get("user") or {}
        author_id = author.get("id")
        labels = [
            label.get("name")
            for label in issue.get("labels") or []
            if isinstance(label, dict) and label.get("name")
        ]
        text = f"{title}\n\n{body}" if body else title
        return RawDocument(
            external_id=f"issue:{full_name}#{number}",
            source_system=self.system,
            uri=issue.get("html_url"),
            title=f"{full_name}#{number}: {title}",
            body=text,
            mime="text/markdown",
            author_external_id=(
                f"{self.system.value}:{author_id}" if author_id is not None else None
            ),
            thread_id=f"github:{full_name}",
            created_at=_instant(issue.get("created_at")),
            modified_at=_instant(issue.get("updated_at")),
            acls=owner_acl(self._context),
            raw_metadata={
                "repo": full_name,
                "kind": "issue",
                "state": issue.get("state"),
                "labels": labels,
            },
        )

    async def acls(self, external_id: str) -> list[AclEntry]:
        return owner_acl(self._context)
