"""OneDrive, Teams and SharePoint connectors against a scripted graph.microsoft.com.
No test may reach a network."""

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
from jutsu_connectors.providers.microsoft import (
    OneDriveConnector,
    SharePointConnector,
    TeamsConnector,
)
from jutsu_core.models import SourceSystem

SUBJECT = "48d31887-5fad-4d73-a9f5-3c356e68a038"
CONTEXT = ProviderContext(namespace=SourceSystem.M365, subject=SUBJECT)
GRAPH = "https://graph.microsoft.com/v1.0"


class StaticToken:
    def __init__(self, value: str = "graph-token") -> None:
        self.value = value

    async def access_token(self) -> str:
        return self.value


def connector_over(cls: Any, handler: Any) -> tuple[Any, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return cls(CONTEXT, StaticToken(), client), client


# --------------------------------------------------------------------------- OneDrive

DOC_ITEM = {
    "id": "01BYE5RZ56Y2GOVW7725BZO354PWSELRRZ",
    "name": "quarterly-notes.md",
    "size": 4523,
    "webUrl": "https://contoso-my.sharepoint.com/personal/megan_contoso_com/Documents/quarterly-notes.md",
    "createdDateTime": "2026-07-14T08:30:00Z",
    "lastModifiedDateTime": "2026-08-28T16:45:00Z",
    "createdBy": {
        "user": {"id": "d4957c9d-b6f5-4b8e-b3a1-2f0f6cb2c1b9", "displayName": "Megan Bowen"}
    },
    "file": {"mimeType": "text/markdown"},
}
FOLDER_ITEM = {
    "id": "01BYE5RZ6TAJHXA5GMWZB2HDLD7SNEXFFU",
    "name": "Attachments",
    "size": 0,
    "createdDateTime": "2026-01-10T09:00:00Z",
    "lastModifiedDateTime": "2026-08-30T10:00:00Z",
    "folder": {"childCount": 3},
}
BINARY_ITEM = {
    "id": "01BYE5RZ4EXQCLXOOZINHZFN3J2NPHNSGN",
    "name": "all-hands-deck.pptx",
    "size": 4_812_331,
    "createdDateTime": "2026-06-01T09:00:00Z",
    "lastModifiedDateTime": "2026-08-29T11:00:00Z",
    "file": {
        "mimeType": "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    },
}
OLD_ITEM = {
    "id": "01BYE5RZ2WMV5TF7DZLFEK2ZUXYCHXG6QA",
    "name": "archive-notes.txt",
    "size": 812,
    "createdDateTime": "2023-11-02T10:00:00Z",
    "lastModifiedDateTime": "2024-02-01T09:00:00Z",
    "file": {"mimeType": "text/plain"},
}
DOC_TEXT = "# Quarterly notes\n\nHiring pauses until the Q3 review lands."
DOWNLOAD_URL = "https://contoso-my.sharepoint.com/_layouts/15/download.aspx?UniqueId=56y2govw"


def onedrive_scripted(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1.0/me/drive/root/delta":
        if request.url.params.get("token") == "page2":
            return httpx.Response(
                200,
                json={
                    "value": [BINARY_ITEM, OLD_ITEM],
                    "@odata.deltaLink": f"{GRAPH}/me/drive/root/delta?token=latest",
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [DOC_ITEM, FOLDER_ITEM],
                "@odata.nextLink": f"{GRAPH}/me/drive/root/delta?token=page2",
            },
        )
    if path == f"/v1.0/me/drive/items/{DOC_ITEM['id']}":
        return httpx.Response(200, json=DOC_ITEM)
    if path == f"/v1.0/me/drive/items/{DOC_ITEM['id']}/content":
        return httpx.Response(302, headers={"Location": DOWNLOAD_URL})
    if request.url.host == "contoso-my.sharepoint.com":
        return httpx.Response(200, text=DOC_TEXT)
    raise AssertionError(f"unexpected call: {path}")


class TestOneDriveListing:
    async def test_lists_text_files_across_two_delta_pages(self) -> None:
        connector, client = connector_over(OneDriveConnector, onedrive_scripted)
        async with client:
            ids = [i async for i in connector.list_since(None)]
        assert ids == [f"item:{DOC_ITEM['id']}", f"item:{OLD_ITEM['id']}"]

    async def test_folders_and_oversized_binaries_are_never_listed(self) -> None:
        connector, client = connector_over(OneDriveConnector, onedrive_scripted)
        async with client:
            ids = [i async for i in connector.list_since(None)]
        assert f"item:{FOLDER_ITEM['id']}" not in ids
        assert f"item:{BINARY_ITEM['id']}" not in ids

    async def test_the_cursor_filters_items_modified_before_it(self) -> None:
        connector, client = connector_over(OneDriveConnector, onedrive_scripted)
        async with client:
            ids = [i async for i in connector.list_since("2026-08-01T00:00:00+00:00")]
        assert ids == [f"item:{DOC_ITEM['id']}"]


class TestOneDriveFetch:
    async def test_a_file_normalises_with_author_and_owner_acl(self) -> None:
        connector, client = connector_over(OneDriveConnector, onedrive_scripted)
        async with client:
            doc = await connector.fetch(f"item:{DOC_ITEM['id']}")
        assert doc.title == "quarterly-notes.md"
        assert "Hiring pauses until the Q3 review lands" in doc.body
        assert doc.mime == "text/markdown"
        assert doc.author_external_id == "m365:d4957c9d-b6f5-4b8e-b3a1-2f0f6cb2c1b9"
        assert doc.thread_id is None
        assert doc.created_at == datetime(2026, 7, 14, 8, 30, tzinfo=UTC)
        assert doc.modified_at == datetime(2026, 8, 28, 16, 45, tzinfo=UTC)
        assert [a.principal_id for a in doc.acls] == [f"m365:{SUBJECT}"]
        assert all(a.permission == "read" for a in doc.acls)

    async def test_the_download_redirect_is_followed_without_the_bearer_token(self) -> None:
        downloads: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.host == "contoso-my.sharepoint.com":
                downloads.append(request)
            return onedrive_scripted(request)

        connector, client = connector_over(OneDriveConnector, recording)
        async with client:
            doc = await connector.fetch(f"item:{DOC_ITEM['id']}")
        assert doc.body == DOC_TEXT
        assert len(downloads) == 1
        assert "Authorization" not in downloads[0].headers

    async def test_an_unrecognised_id_shape_is_permanent(self) -> None:
        connector, client = connector_over(OneDriveConnector, onedrive_scripted)
        async with client:
            with pytest.raises(ProviderApiError) as excinfo:
                await connector.fetch("drive:whatever")
        assert excinfo.value.transient is False


# ----------------------------------------------------------------------------- Teams

CHAT_ID = "19:2da4c29f6d7041eca70b638b43d45437@thread.v2"
CHAT = {"id": CHAT_ID, "topic": "Q3 planning", "chatType": "group"}
MESSAGE = {
    "id": "1756392300000",
    "messageType": "message",
    "createdDateTime": "2026-08-28T14:45:00Z",
    "lastModifiedDateTime": "2026-08-28T14:45:00Z",
    "from": {"user": {"id": "ee0d0a5d-19e1-41c9-b44e-2c1a4c2c8f9b", "displayName": "Nestor Wilke"}},
    "body": {
        "contentType": "html",
        "content": "<p>Budget draft is <b>ready</b> for review &amp; sign-off.</p>",
    },
}
SYSTEM_EVENT = {
    "id": "1756392200000",
    "messageType": "systemEventMessage",
    "createdDateTime": "2026-08-28T14:40:00Z",
    "body": {"contentType": "html", "content": "<systemEventMessage/>"},
}
EMPTY_MESSAGE = {
    "id": "1756392100000",
    "messageType": "message",
    "createdDateTime": "2026-08-28T14:30:00Z",
    "body": {"contentType": "text", "content": ""},
}
OLD_MESSAGE = {
    "id": "1701432000000",
    "messageType": "message",
    "createdDateTime": "2023-12-01T12:00:00Z",
    "lastModifiedDateTime": "2023-12-01T12:00:00Z",
    "from": {"user": {"id": "ee0d0a5d-19e1-41c9-b44e-2c1a4c2c8f9b"}},
    "body": {"contentType": "html", "content": "<p>Archived thread.</p>"},
}


def teams_scripted(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1.0/me/chats":
        return httpx.Response(200, json={"value": [CHAT]})
    if path == f"/v1.0/me/chats/{CHAT_ID}/messages":
        if request.url.params.get("$skiptoken") == "m2":
            return httpx.Response(200, json={"value": [EMPTY_MESSAGE, OLD_MESSAGE]})
        return httpx.Response(
            200,
            json={
                "value": [MESSAGE, SYSTEM_EVENT],
                "@odata.nextLink": f"{GRAPH}/me/chats/{CHAT_ID}/messages?$skiptoken=m2",
            },
        )
    if path == f"/v1.0/me/chats/{CHAT_ID}/messages/{MESSAGE['id']}":
        return httpx.Response(200, json=MESSAGE)
    if path == f"/v1.0/me/chats/{CHAT_ID}":
        return httpx.Response(200, json=CHAT)
    raise AssertionError(f"unexpected call: {path}")


class TestTeamsListing:
    async def test_lists_messages_across_pages_and_never_system_events(self) -> None:
        connector, client = connector_over(TeamsConnector, teams_scripted)
        async with client:
            ids = [i async for i in connector.list_since(None)]
        assert ids == [
            f"chat:{CHAT_ID}:{MESSAGE['id']}",
            f"chat:{CHAT_ID}:{OLD_MESSAGE['id']}",
        ]
        assert not any(str(SYSTEM_EVENT["id"]) in i for i in ids)
        assert not any(str(EMPTY_MESSAGE["id"]) in i for i in ids)

    async def test_the_cursor_filters_messages_modified_before_it(self) -> None:
        connector, client = connector_over(TeamsConnector, teams_scripted)
        async with client:
            ids = [i async for i in connector.list_since("2026-08-01T00:00:00+00:00")]
        assert ids == [f"chat:{CHAT_ID}:{MESSAGE['id']}"]


class TestTeamsFetch:
    async def test_html_flattens_and_the_topic_titles_the_message(self) -> None:
        connector, client = connector_over(TeamsConnector, teams_scripted)
        async with client:
            doc = await connector.fetch(f"chat:{CHAT_ID}:{MESSAGE['id']}")
        assert doc.body == "Budget draft is ready for review & sign-off."
        assert doc.title == "Q3 planning"
        assert doc.author_external_id == "m365:ee0d0a5d-19e1-41c9-b44e-2c1a4c2c8f9b"
        assert doc.thread_id == f"m365-chat:{CHAT_ID}"
        assert doc.created_at == datetime(2026, 8, 28, 14, 45, tzinfo=UTC)
        assert [a.principal_id for a in doc.acls] == [f"m365:{SUBJECT}"]
        assert all(a.permission == "read" for a in doc.acls)

    async def test_the_chat_topic_is_fetched_once_per_connector(self) -> None:
        topic_calls: list[str] = []

        def counting(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/v1.0/me/chats/{CHAT_ID}":
                topic_calls.append(request.url.path)
            return teams_scripted(request)

        connector, client = connector_over(TeamsConnector, counting)
        async with client:
            await connector.fetch(f"chat:{CHAT_ID}:{MESSAGE['id']}")
            await connector.fetch(f"chat:{CHAT_ID}:{MESSAGE['id']}")
        assert len(topic_calls) == 1

    async def test_a_topicless_chat_falls_back_to_a_generic_title(self) -> None:
        def topicless(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/v1.0/me/chats/{CHAT_ID}":
                return httpx.Response(
                    200, json={"id": CHAT_ID, "topic": None, "chatType": "oneOnOne"}
                )
            return teams_scripted(request)

        connector, client = connector_over(TeamsConnector, topicless)
        async with client:
            doc = await connector.fetch(f"chat:{CHAT_ID}:{MESSAGE['id']}")
        assert doc.title == "Chat message"

    async def test_an_unrecognised_id_shape_is_permanent(self) -> None:
        connector, client = connector_over(TeamsConnector, teams_scripted)
        async with client:
            with pytest.raises(ProviderApiError) as excinfo:
                await connector.fetch("chat:1756392300000")
        assert excinfo.value.transient is False


# ------------------------------------------------------------------------ SharePoint

SITE_A_ID = "contoso.sharepoint.com,5a58bb09-1fba-41c1-8125-69da264370a0,9f2ec1da-4be4-4c8a-a26d-8f232cd4f52a"
SITE_B_ID = "contoso.sharepoint.com,7d40cbb6-05a2-4c47-b04a-e7de1c1e8f52,3b9f8a1c-2f70-4b5f-9a5e-0d3c2b1a4e6f"
SITE_EMPTY_ID = "contoso.sharepoint.com,1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f,6f5e4d3c-2b1a-4f9e-8d7c-6b5a4c3d2e1f"
SITE_A = {"id": SITE_A_ID, "displayName": "Operations"}
SITE_B = {"id": SITE_B_ID, "displayName": "Engineering"}
SITE_EMPTY = {"id": SITE_EMPTY_ID, "displayName": "Fresh Team Site"}
SITE_A_FILE = {
    "id": "01AOPS3PLBQ7GJ4NN2ANBKVFP7RA3SRJ6E",
    "name": "incident-runbook.md",
    "size": 5120,
    "webUrl": "https://contoso.sharepoint.com/sites/operations/Shared%20Documents/incident-runbook.md",
    "createdDateTime": "2026-05-01T09:00:00Z",
    "lastModifiedDateTime": "2026-08-25T10:15:00Z",
    "createdBy": {
        "user": {"id": "9a1f6c2e-8d4b-4f3a-b7e1-5c2d9e8f7a6b", "displayName": "Alex Wilber"}
    },
    "file": {"mimeType": "text/markdown"},
}
SITE_B_FILE = {
    "id": "01BENGR7Y2XKQ5MPO3EJH2BDLXWFVUS44Q",
    "name": "oncall-rota.csv",
    "size": 2048,
    "createdDateTime": "2026-06-10T07:00:00Z",
    "lastModifiedDateTime": "2026-08-20T18:00:00Z",
    "file": {"mimeType": "text/csv"},
}
RUNBOOK_TEXT = "# Incident runbook\n\nPage the graph owner before restarting Neo4j."


def sharepoint_scripted(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1.0/sites":
        return httpx.Response(200, json={"value": [SITE_A, SITE_EMPTY, SITE_B]})
    if path == f"/v1.0/sites/{SITE_A_ID}/drive/root/delta":
        return httpx.Response(
            200,
            json={
                "value": [SITE_A_FILE],
                "@odata.deltaLink": f"{GRAPH}/sites/{SITE_A_ID}/drive/root/delta?token=a",
            },
        )
    if path == f"/v1.0/sites/{SITE_EMPTY_ID}/drive/root/delta":
        return httpx.Response(
            404,
            json={"error": {"code": "itemNotFound", "message": "The resource could not be found."}},
        )
    if path == f"/v1.0/sites/{SITE_B_ID}/drive/root/delta":
        return httpx.Response(
            200,
            json={
                "value": [SITE_B_FILE],
                "@odata.deltaLink": f"{GRAPH}/sites/{SITE_B_ID}/drive/root/delta?token=b",
            },
        )
    if path == f"/v1.0/sites/{SITE_A_ID}/drive/items/{SITE_A_FILE['id']}":
        return httpx.Response(200, json=SITE_A_FILE)
    if path == f"/v1.0/sites/{SITE_A_ID}/drive/items/{SITE_A_FILE['id']}/content":
        return httpx.Response(200, text=RUNBOOK_TEXT)
    raise AssertionError(f"unexpected call: {path}")


class TestSharePoint:
    async def test_two_sites_each_contribute_their_files(self) -> None:
        # SITE_EMPTY sits between A and B, so reaching B's file proves the walk
        # continued past the site without a document library.
        connector, client = connector_over(SharePointConnector, sharepoint_scripted)
        async with client:
            ids = [i async for i in connector.list_since(None)]
        assert ids == [
            f"site:{SITE_A_ID}:{SITE_A_FILE['id']}",
            f"site:{SITE_B_ID}:{SITE_B_FILE['id']}",
        ]

    async def test_at_most_ten_sites_are_walked(self) -> None:
        sites = [
            {
                "id": f"contoso.sharepoint.com,00000000-0000-0000-0000-0000000000{i:02d},"
                "11111111-1111-1111-1111-111111111111",
                "displayName": f"Site {i}",
            }
            for i in range(12)
        ]
        delta_calls: list[str] = []

        def many_sites(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1.0/sites":
                return httpx.Response(200, json={"value": sites})
            if request.url.path.endswith("/drive/root/delta"):
                delta_calls.append(request.url.path)
                return httpx.Response(200, json={"value": []})
            raise AssertionError(f"unexpected call: {request.url.path}")

        connector, client = connector_over(SharePointConnector, many_sites)
        async with client:
            ids = [i async for i in connector.list_since(None)]
        assert ids == []
        assert len(delta_calls) == 10

    async def test_a_dead_token_on_a_site_drive_still_fails_the_walk(self) -> None:
        def unauthorized_site(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1.0/sites":
                return httpx.Response(200, json={"value": [SITE_A]})
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})

        connector, client = connector_over(SharePointConnector, unauthorized_site)
        async with client:
            with pytest.raises(ProviderAuthError):
                _ = [i async for i in connector.list_since(None)]

    async def test_a_site_file_normalises_with_the_site_as_thread(self) -> None:
        connector, client = connector_over(SharePointConnector, sharepoint_scripted)
        async with client:
            doc = await connector.fetch(f"site:{SITE_A_ID}:{SITE_A_FILE['id']}")
        assert doc.title == "incident-runbook.md"
        assert "Page the graph owner" in doc.body
        assert doc.author_external_id == "m365:9a1f6c2e-8d4b-4f3a-b7e1-5c2d9e8f7a6b"
        assert doc.thread_id == f"m365-site:{SITE_A_ID}"
        assert doc.created_at == datetime(2026, 5, 1, 9, tzinfo=UTC)
        assert [a.principal_id for a in doc.acls] == [f"m365:{SUBJECT}"]
        assert all(a.permission == "read" for a in doc.acls)

    async def test_an_unrecognised_id_shape_is_permanent(self) -> None:
        connector, client = connector_over(SharePointConnector, sharepoint_scripted)
        async with client:
            with pytest.raises(ProviderApiError) as excinfo:
                await connector.fetch("library:whatever")
        assert excinfo.value.transient is False


# ------------------------------------------------------------------ failure taxonomy


class TestFailureTaxonomy:
    async def test_a_rate_limit_is_transient_and_carries_retry_after(self) -> None:
        def limited(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "30"}, json={})

        connector, client = connector_over(OneDriveConnector, limited)
        async with client:
            with pytest.raises(ProviderApiError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert excinfo.value.transient is True
        assert excinfo.value.retry_after == 30.0

    async def test_a_dead_token_is_an_auth_error_not_a_retry(self) -> None:
        def unauthorized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})

        connector, client = connector_over(TeamsConnector, unauthorized)
        async with client:
            with pytest.raises(ProviderAuthError):
                _ = [i async for i in connector.list_since(None)]

    async def test_no_error_text_ever_carries_the_token(self) -> None:
        def unauthorized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})

        connector, client = connector_over(SharePointConnector, unauthorized)
        async with client:
            with pytest.raises(ProviderAuthError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert "graph-token" not in json.dumps(str(excinfo.value))
