"""GitHubConnector against a scripted api.github.com. No test may reach a network."""

from __future__ import annotations

import json
from collections.abc import Callable
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


class TestARepositoryWithNoReadme:
    """Most repositories do not have one, and the listing cannot tell in advance.

    `list_since` yields `readme:<repo>` for every repository it sees, because
    `GET /user/repos` carries no field saying whether a README exists. The fetch then
    404s, which the shared HTTP layer classifies as a permanent rejection — so every
    README-less repository became a job that failed for ever and sat in the view an
    administrator reads to find real problems.
    """

    async def test_a_missing_readme_is_absence_rather_than_failure(self) -> None:
        from jutsu_connectors.providers.base import DocumentGone

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/acme/tooling":
                return httpx.Response(200, json={"html_url": "https://github.com/acme/tooling"})
            if request.url.path == "/repos/acme/tooling/readme":
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(404, json={})

        connector, client = connector_over(handler)
        async with client:
            with pytest.raises(DocumentGone):
                await connector.fetch("readme:acme/tooling")

    async def test_a_rate_limited_readme_is_still_a_retryable_failure(self) -> None:
        """The absence branch must not swallow a transient refusal — a 429 here means
        come back later, not "this repository has no README"."""
        from jutsu_connectors.providers.base import ProviderApiError

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/acme/tooling":
                return httpx.Response(200, json={"html_url": "https://github.com/acme/tooling"})
            return httpx.Response(429, headers={"Retry-After": "30"}, json={})

        connector, client = connector_over(handler)
        async with client:
            with pytest.raises(ProviderApiError) as raised:
                await connector.fetch("readme:acme/tooling")
        assert raised.value.transient is True


class TestARevokedGrantIsNotAMissingReadme:
    """The laundering this class exists to prevent.

    `ProviderAuthError` subclasses `ProviderApiError` with `transient=False`, so a branch
    that converted every non-transient error into `DocumentGone` turned a revoked grant
    into "this repository has no README": the job completed, the connection went on
    calling itself connected, and the employee was never asked to reconnect. Only a
    genuine 404 may convert.
    """

    def _repo_then(self, status: int) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/acme/tooling":
                return httpx.Response(200, json={"html_url": "https://github.com/acme/tooling"})
            return httpx.Response(status, json={"message": "nope"})

        return handler

    async def test_a_403_stays_an_auth_error(self) -> None:
        from jutsu_connectors.providers.base import DocumentGone, ProviderAuthError

        connector, client = connector_over(self._repo_then(403))
        async with client:
            with pytest.raises(ProviderAuthError) as raised:
                await connector.fetch("readme:acme/tooling")
        assert not isinstance(raised.value, DocumentGone)

    async def test_a_401_stays_an_auth_error(self) -> None:
        from jutsu_connectors.providers.base import DocumentGone, ProviderAuthError

        connector, client = connector_over(self._repo_then(401))
        async with client:
            with pytest.raises(ProviderAuthError) as raised:
                await connector.fetch("readme:acme/tooling")
        assert not isinstance(raised.value, DocumentGone)

    async def test_another_permanent_4xx_is_not_an_absence_either(self) -> None:
        """A 422 is the provider refusing the request, not saying the file is missing."""
        from jutsu_connectors.providers.base import DocumentGone, ProviderApiError

        connector, client = connector_over(self._repo_then(422))
        async with client:
            with pytest.raises(ProviderApiError) as raised:
                await connector.fetch("readme:acme/tooling")
        assert not isinstance(raised.value, DocumentGone)
        assert raised.value.status == 422

    async def test_only_a_404_converts(self) -> None:
        from jutsu_connectors.providers.base import DocumentGone

        connector, client = connector_over(self._repo_then(404))
        async with client:
            with pytest.raises(DocumentGone):
                await connector.fetch("readme:acme/tooling")
