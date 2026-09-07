"""A bounded listing must say it was bounded. No test here reaches a network.

Every connector's page loop is deliberately bounded — an unbounded `while nextPageToken`
against a looping or hostile API is a walk that never returns — and every one of them
used to reach that bound by falling out of a `for ... in range(...)`, which the caller
cannot distinguish from the provider running out of results. The walk then advanced the
source cursor to the moment it started, so everything past the bound was filtered out of
the next listing by that cursor and never seen again.

These tests script a provider that always offers another page, and assert that the
connector raises rather than returning. They are the only place that behaviour is
observable: with a real-sized fixture the bound is never reached, and with a truncating
connector the walk's own counters look perfectly healthy.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from jutsu_connectors.providers.atlassian import ConfluenceConnector, JiraConnector
from jutsu_connectors.providers.base import ListingIncomplete, ProviderContext
from jutsu_connectors.providers.github import GitHubConnector
from jutsu_connectors.providers.google import GmailConnector, GoogleDriveConnector
from jutsu_connectors.providers.slack import SlackConnector
from jutsu_connectors.providers.zoom import ZoomConnector
from jutsu_core.models import SourceSystem


class StaticToken:
    async def access_token(self) -> str:
        return "tok"


def client_over(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def context(namespace: SourceSystem, subject: str = "subject-1") -> ProviderContext:
    return ProviderContext(namespace=namespace, subject=subject)


async def drain(connector: Any) -> int:
    """List everything, returning how many identifiers arrived before the refusal."""
    seen = 0
    async for _ in connector.list_since(None):
        seen += 1
    return seen


class TestEveryProviderRefusesToStopQuietly:
    """One scripted endpoint per provider, each offering a next page for ever."""

    async def test_gmail(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "messages": [{"id": "m1"}, {"id": "m2"}],
                    "nextPageToken": "always-another",
                },
            )

        connector = GmailConnector(context(SourceSystem.GMAIL), StaticToken(), client_over(handler))
        with pytest.raises(ListingIncomplete):
            await drain(connector)

    async def test_google_drive(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "files": [{"id": "f1", "modifiedTime": "2026-01-01T00:00:00Z"}],
                    "nextPageToken": "always-another",
                },
            )

        connector = GoogleDriveConnector(
            context(SourceSystem.GMAIL), StaticToken(), client_over(handler)
        )
        with pytest.raises(ListingIncomplete):
            await drain(connector)

    async def test_github(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # A full page every time is GitHub's "there is more" signal.
            return httpx.Response(
                200,
                json=[
                    {"full_name": f"acme/repo-{index}", "pushed_at": "2026-01-01T00:00:00Z"}
                    for index in range(100)
                ],
            )

        connector = GitHubConnector(
            context(SourceSystem.GITHUB), StaticToken(), client_over(handler)
        )
        with pytest.raises(ListingIncomplete):
            await drain(connector)

    async def test_slack(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [{"id": "C1", "name": "general"}],
                    "response_metadata": {"next_cursor": "always-another"},
                },
            )

        connector = SlackConnector(context(SourceSystem.SLACK), StaticToken(), client_over(handler))
        with pytest.raises(ListingIncomplete):
            await drain(connector)

    async def test_jira(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "accessible-resources" in str(request.url):
                return httpx.Response(200, json=[{"id": "cloud-1", "url": "https://x"}])
            return httpx.Response(
                200,
                json={
                    "issues": [
                        {
                            "key": f"ENG-{index}",
                            "fields": {"updated": "2026-01-01T00:00:00.000+0000"},
                        }
                        for index in range(50)
                    ],
                    # Token-paged, and there is always another page.
                    "isLast": False,
                    "nextPageToken": "always-another",
                },
            )

        connector = JiraConnector(context(SourceSystem.JIRA), StaticToken(), client_over(handler))
        with pytest.raises(ListingIncomplete):
            await drain(connector)

    async def test_confluence(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "accessible-resources" in str(request.url):
                return httpx.Response(200, json=[{"id": "cloud-1", "url": "https://x"}])
            return httpx.Response(
                200,
                json={
                    "results": [{"id": str(index)} for index in range(50)],
                    "size": 50,
                    "limit": 50,
                    "_links": {"next": "/rest/api/content/search?start=50"},
                },
            )

        connector = ConfluenceConnector(
            context(SourceSystem.CONFLUENCE), StaticToken(), client_over(handler)
        )
        with pytest.raises(ListingIncomplete):
            await drain(connector)

    async def test_zoom(self) -> None:
        """Zoom's bound sits inside a date window, and stepping past it is worse than
        the others: the walk would move to the *next* window with pages still unread in
        this one, so the gap is in the middle of the history rather than at its end."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "meetings": [{"uuid": f"uuid-{index}"} for index in range(30)],
                    "next_page_token": "always-another",
                },
            )

        connector = ZoomConnector(context(SourceSystem.ZOOM), StaticToken(), client_over(handler))
        with pytest.raises(ListingIncomplete):
            await drain(connector)


class TestTheRefusalCarriesNoContent:
    async def test_the_message_is_counts_and_nothing_else(self) -> None:
        """It is stored on the job row and logged, so §4.9 applies to it exactly as it
        applies to every other error string the worker writes."""
        error = ListingIncomplete(4321, pages=50)

        assert "4321" in str(error)
        assert "50" in str(error)
        assert error.listed == 4321
        assert error.pages == 50
