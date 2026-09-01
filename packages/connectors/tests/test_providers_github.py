"""GitHubConnector against a scripted api.github.com. No test may reach a network."""

from __future__ import annotations

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
from jutsu_connectors.providers.github import GitHubConnector
from jutsu_core.models import SourceSystem

CONTEXT = ProviderContext(namespace=SourceSystem.GITHUB, subject="583231")


class StaticToken:
    def __init__(self, value: str = "gh-token") -> None:
        self.value = value
        self.calls = 0

    async def access_token(self) -> str:
        self.calls += 1
        return self.value


def connector_over(handler: Any) -> tuple[GitHubConnector, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GitHubConnector(CONTEXT, StaticToken(), client), client


REPO = {
    "full_name": "octocat/hello",
    "html_url": "https://github.com/octocat/hello",
    "created_at": "2024-01-05T10:00:00Z",
    "pushed_at": "2026-08-30T09:00:00Z",
}
OLD_REPO = {
    "full_name": "octocat/ancient",
    "html_url": "https://github.com/octocat/ancient",
    "created_at": "2019-01-05T10:00:00Z",
    "pushed_at": "2020-01-01T00:00:00Z",
}
ISSUE = {
    "number": 7,
    "title": "Retry ladder skips Retry-After",
    "body": "The scheduler ignores the header entirely.",
    "state": "open",
    "html_url": "https://github.com/octocat/hello/issues/7",
    "user": {"id": 583231, "login": "octocat"},
    "labels": [{"name": "bug"}],
    "created_at": "2026-08-20T12:00:00Z",
    "updated_at": "2026-08-29T12:00:00Z",
}


def scripted(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/user/repos":
        return httpx.Response(200, json=[REPO, OLD_REPO])
    if path == "/repos/octocat/hello/issues":
        return httpx.Response(200, json=[ISSUE])
    if path == "/repos/octocat/ancient/issues":
        return httpx.Response(200, json=[])
    if path == "/repos/octocat/hello/issues/7":
        return httpx.Response(200, json=ISSUE)
    if path == "/repos/octocat/hello/readme":
        return httpx.Response(200, text="# hello\nreal readme text")
    if path == "/repos/octocat/hello":
        return httpx.Response(200, json=REPO)
    raise AssertionError(f"unexpected call: {path}")


class TestListing:
    async def test_lists_readme_and_issues_newest_repos_first(self) -> None:
        connector, client = connector_over(scripted)
        async with client:
            ids = [i async for i in connector.list_since(None)]
        assert ids == ["readme:octocat/hello", "issue:octocat/hello#7", "readme:octocat/ancient"]

    async def test_the_cursor_stops_at_the_first_repo_older_than_it(self) -> None:
        connector, client = connector_over(scripted)
        async with client:
            ids = [i async for i in connector.list_since("2026-08-01T00:00:00+00:00")]
        assert "readme:octocat/ancient" not in ids
        assert "readme:octocat/hello" in ids

    async def test_the_cursor_is_forwarded_as_since_for_issues(self) -> None:
        seen: list[str] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/octocat/hello/issues":
                seen.append(request.url.params.get("since", ""))
            return scripted(request)

        connector, client = connector_over(recording)
        async with client:
            _ = [i async for i in connector.list_since("2026-08-01T00:00:00+00:00")]
        assert seen == ["2026-08-01T00:00:00+00:00"]


class TestFetch:
    async def test_an_issue_normalises_with_author_thread_and_owner_acl(self) -> None:
        connector, client = connector_over(scripted)
        async with client:
            doc = await connector.fetch("issue:octocat/hello#7")
        assert doc.title == "octocat/hello#7: Retry ladder skips Retry-After"
        assert "scheduler ignores the header" in doc.body
        assert doc.author_external_id == "github:583231"
        assert doc.thread_id == "github:octocat/hello"
        assert doc.created_at == datetime(2026, 8, 20, 12, tzinfo=UTC)
        assert [a.principal_id for a in doc.acls] == ["github:583231"]
        assert all(a.permission == "read" for a in doc.acls)

    async def test_a_readme_fetches_raw_text(self) -> None:
        connector, client = connector_over(scripted)
        async with client:
            doc = await connector.fetch("readme:octocat/hello")
        assert doc.body == "# hello\nreal readme text"
        assert doc.external_id == "readme:octocat/hello"

    async def test_an_unrecognised_id_shape_is_permanent(self) -> None:
        connector, client = connector_over(scripted)
        async with client:
            with pytest.raises(ProviderApiError) as excinfo:
                await connector.fetch("gist:whatever")
        assert excinfo.value.transient is False


class TestFailureTaxonomy:
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
            return httpx.Response(401, json={"message": "Bad credentials"})

        connector, client = connector_over(unauthorized)
        async with client:
            with pytest.raises(ProviderAuthError):
                _ = [i async for i in connector.list_since(None)]

    async def test_no_error_text_ever_carries_the_token(self) -> None:
        def unauthorized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Bad credentials"})

        connector, client = connector_over(unauthorized)
        async with client:
            with pytest.raises(ProviderAuthError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert "gh-token" not in json.dumps(str(excinfo.value))
