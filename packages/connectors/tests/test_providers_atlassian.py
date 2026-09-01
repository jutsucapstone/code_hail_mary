"""Jira and Confluence connectors against a scripted api.atlassian.com.

No test may reach a network. Both connectors share one accessible-resources
fixture, because they share the resolution.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from jutsu_connectors.providers.atlassian import (
    ConfluenceConnector,
    JiraConnector,
    _adf_text,
    _html_text,
)
from jutsu_connectors.providers.base import (
    ProviderApiError,
    ProviderAuthError,
    ProviderContext,
)
from jutsu_core.models import Connector, SourceSystem

ACCOUNT_ID = "5b10ac8d82e05b22cc7d4ef5"
JIRA_CONTEXT = ProviderContext(namespace=SourceSystem.JIRA, subject=ACCOUNT_ID)
CONFLUENCE_CONTEXT = ProviderContext(namespace=SourceSystem.CONFLUENCE, subject=ACCOUNT_ID)

CLOUD_ID = "11223344-5566-7788-99aa-bbccddeeff00"
RESOURCES = [
    {
        "id": CLOUD_ID,
        "url": "https://synthetic-corp.atlassian.net",
        "name": "synthetic-corp",
        "scopes": ["read:jira-work"],
    }
]


class StaticToken:
    def __init__(self, value: str = "atl-token") -> None:
        self.value = value
        self.calls = 0

    async def access_token(self) -> str:
        self.calls += 1
        return self.value


def jira_over(handler: Any) -> tuple[JiraConnector, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JiraConnector(JIRA_CONTEXT, StaticToken(), client), client


def confluence_over(handler: Any) -> tuple[ConfluenceConnector, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ConfluenceConnector(CONFLUENCE_CONTEXT, StaticToken(), client), client


ADF_DESCRIPTION = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "The scheduler ignores "},
                {"type": "text", "text": "Retry-After", "marks": [{"type": "code"}]},
                {"type": "text", "text": " entirely."},
            ],
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "jobs retry immediately"}],
                        }
                    ],
                },
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "quota drains twice as fast"}],
                        }
                    ],
                },
            ],
        },
    ],
}

JIRA_ISSUE = {
    "id": "10042",
    "key": "ENG-7",
    "fields": {
        "summary": "Retry ladder skips Retry-After",
        "description": ADF_DESCRIPTION,
        "created": "2026-08-20T12:00:00.000+0000",
        "updated": "2026-08-29T09:30:00.000+0000",
        "reporter": {"accountId": ACCOUNT_ID, "displayName": "Synthetic Reporter"},
        "assignee": {
            "accountId": "70121:aa61e8bc-9f34-4c1e-8d51-1e0e2f7f0001",
            "displayName": "Synthetic Assignee",
        },
        "status": {"name": "In Progress"},
        "project": {"key": "ENG", "name": "Engineering"},
    },
}

CONFLUENCE_PAGE = {
    "id": "98305",
    "type": "page",
    "status": "current",
    "title": "Retry ladder runbook",
    "space": {"key": "ENG", "name": "Engineering"},
    "history": {
        "createdDate": "2026-07-01T09:30:00.000Z",
        "createdBy": {"accountId": ACCOUNT_ID, "displayName": "Synthetic Author"},
    },
    "version": {"number": 4, "when": "2026-08-25T10:00:00.000Z"},
    "body": {
        "storage": {
            "representation": "storage",
            "value": (
                "<h1>Runbook</h1><p>Honour <code>Retry-After</code> &amp; back off.</p>"
                "<ul><li>check quota<ul><li>then the ladder</li></ul></li></ul>"
            ),
        }
    },
    "_links": {
        "base": "https://synthetic-corp.atlassian.net/wiki",
        "webui": "/spaces/ENG/pages/98305/Retry+ladder+runbook",
    },
}


def scripted(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/oauth/token/accessible-resources":
        return httpx.Response(200, json=RESOURCES)
    if path == f"/ex/jira/{CLOUD_ID}/rest/api/3/search":
        start_at = int(request.url.params.get("startAt", "0"))
        if start_at == 0:
            return httpx.Response(
                200,
                json={
                    "startAt": 0,
                    "maxResults": 50,
                    "total": 3,
                    "issues": [
                        {"key": "ENG-5", "fields": {"updated": "2026-08-27T08:00:00.000+0000"}},
                        {"key": "ENG-6", "fields": {"updated": "2026-08-28T08:00:00.000+0000"}},
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "startAt": start_at,
                "maxResults": 50,
                "total": 3,
                "issues": [{"key": "ENG-7", "fields": {"updated": "2026-08-29T09:30:00.000+0000"}}],
            },
        )
    if path == f"/ex/jira/{CLOUD_ID}/rest/api/3/issue/ENG-7":
        return httpx.Response(200, json=JIRA_ISSUE)
    if path == f"/ex/confluence/{CLOUD_ID}/wiki/rest/api/content/search":
        start = int(request.url.params.get("start", "0"))
        if start == 0:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "98305", "type": "page", "title": "Retry ladder runbook"},
                        {"id": "98306", "type": "page", "title": "Quota accounting"},
                    ],
                    "start": 0,
                    "limit": 2,
                    "size": 2,
                    "_links": {"next": "/rest/api/content/search?cursor=synthetic&start=2"},
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [{"id": "98307", "type": "page", "title": "Escalation ladder"}],
                "start": 2,
                "limit": 2,
                "size": 1,
                "_links": {},
            },
        )
    if path == f"/ex/confluence/{CLOUD_ID}/wiki/rest/api/content/98305":
        return httpx.Response(200, json=CONFLUENCE_PAGE)
    raise AssertionError(f"unexpected call: {path}")


class TestFlattening:
    def test_adf_flattens_a_nested_document(self) -> None:
        assert _adf_text(ADF_DESCRIPTION) == (
            "The scheduler ignores Retry-After entirely.\n"
            "jobs retry immediately\n"
            "quota drains twice as fast"
        )

    def test_adf_tolerates_a_missing_description(self) -> None:
        assert _adf_text(None) == ""

    def test_xhtml_strips_tags_unescapes_entities_and_flattens_nested_lists(self) -> None:
        value = CONFLUENCE_PAGE["body"]["storage"]["value"]  # type: ignore[index]
        assert _html_text(value) == (
            "Runbook Honour Retry-After & back off. check quota then the ladder"
        )


class TestJiraListing:
    async def test_lists_issue_keys_across_startat_pages(self) -> None:
        seen: list[tuple[str, str]] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/api/3/search"):
                seen.append(
                    (request.url.params.get("startAt", ""), request.url.params.get("jql", ""))
                )
            return scripted(request)

        connector, client = jira_over(recording)
        async with client:
            ids = [i async for i in connector.list_since(None)]
        assert ids == ["issue:ENG-5", "issue:ENG-6", "issue:ENG-7"]
        assert [start_at for start_at, _ in seen] == ["0", "2"]
        assert all(jql == "order by updated asc" for _, jql in seen)

    async def test_the_cursor_filters_issues_older_than_it(self) -> None:
        connector, client = jira_over(scripted)
        async with client:
            ids = [i async for i in connector.list_since("2026-08-28T00:00:00+00:00")]
        assert "issue:ENG-5" not in ids
        assert "issue:ENG-6" in ids
        assert "issue:ENG-7" in ids

    async def test_the_cursor_is_rendered_into_jql_verbatim(self) -> None:
        seen: list[str] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/api/3/search"):
                seen.append(request.url.params.get("jql", ""))
            return scripted(request)

        connector, client = jira_over(recording)
        async with client:
            _ = [i async for i in connector.list_since("2026-08-28T00:00:00+00:00")]
        assert seen[0] == 'updated >= "2026-08-28 00:00" order by updated asc'


class TestJiraFetch:
    async def test_an_issue_normalises_with_author_thread_and_owner_acl(self) -> None:
        connector, client = jira_over(scripted)
        async with client:
            doc = await connector.fetch("issue:ENG-7")
        assert doc.title == "ENG-7: Retry ladder skips Retry-After"
        assert doc.body.startswith("Retry ladder skips Retry-After\n\n")
        assert "The scheduler ignores Retry-After entirely." in doc.body
        assert "jobs retry immediately\nquota drains twice as fast" in doc.body
        assert doc.author_external_id == f"jira:{ACCOUNT_ID}"
        assert doc.thread_id == "jira:ENG"
        assert doc.created_at == datetime(2026, 8, 20, 12, tzinfo=UTC)
        assert doc.modified_at == datetime(2026, 8, 29, 9, 30, tzinfo=UTC)
        assert [a.principal_id for a in doc.acls] == [f"jira:{ACCOUNT_ID}"]
        assert all(a.permission == "read" for a in doc.acls)
        assert doc.raw_metadata["status"] == "In Progress"
        assert doc.raw_metadata["assignee"] == "70121:aa61e8bc-9f34-4c1e-8d51-1e0e2f7f0001"

    async def test_an_unrecognised_id_shape_is_permanent(self) -> None:
        connector, client = jira_over(scripted)
        async with client:
            for external_id in ("page:98305", "issue:", "issue:not a key"):
                with pytest.raises(ProviderApiError) as excinfo:
                    await connector.fetch(external_id)
                assert excinfo.value.transient is False


class TestConfluenceListing:
    async def test_lists_page_ids_until_the_next_link_runs_out(self) -> None:
        seen: list[tuple[str, str]] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/api/content/search"):
                seen.append(
                    (request.url.params.get("start", ""), request.url.params.get("cql", ""))
                )
            return scripted(request)

        connector, client = confluence_over(recording)
        async with client:
            ids = [i async for i in connector.list_since(None)]
        assert ids == ["page:98305", "page:98306", "page:98307"]
        assert [start for start, _ in seen] == ["0", "2"]
        assert all(cql == "type=page order by lastmodified asc" for _, cql in seen)

    async def test_the_cursor_is_rendered_into_cql_verbatim(self) -> None:
        seen: list[str] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/api/content/search"):
                seen.append(request.url.params.get("cql", ""))
            return scripted(request)

        connector, client = confluence_over(recording)
        async with client:
            _ = [i async for i in connector.list_since("2026-08-28T00:00:00+00:00")]
        assert seen[0] == (
            'type=page and lastmodified >= "2026/08/28 00:00" order by lastmodified asc'
        )


class TestConfluenceFetch:
    async def test_a_page_normalises_with_author_space_and_owner_acl(self) -> None:
        connector, client = confluence_over(scripted)
        async with client:
            doc = await connector.fetch("page:98305")
        assert doc.title == "Retry ladder runbook"
        assert "Honour Retry-After & back off." in doc.body
        assert "<p>" not in doc.body
        assert "check quota then the ladder" in doc.body
        assert doc.author_external_id == f"confluence:{ACCOUNT_ID}"
        assert doc.thread_id == "confluence:ENG"
        assert doc.created_at == datetime(2026, 7, 1, 9, 30, tzinfo=UTC)
        assert doc.modified_at == datetime(2026, 8, 25, 10, tzinfo=UTC)
        assert doc.uri == (
            "https://synthetic-corp.atlassian.net/wiki/spaces/ENG/pages/98305/Retry+ladder+runbook"
        )
        assert [a.principal_id for a in doc.acls] == [f"confluence:{ACCOUNT_ID}"]
        assert all(a.permission == "read" for a in doc.acls)

    async def test_an_unrecognised_id_shape_is_permanent(self) -> None:
        connector, client = confluence_over(scripted)
        async with client:
            for external_id in ("issue:ENG-7", "page:", "page:../secrets"):
                with pytest.raises(ProviderApiError) as excinfo:
                    await connector.fetch(external_id)
                assert excinfo.value.transient is False


class TestCloudResolution:
    async def test_accessible_resources_is_called_once_per_connector_instance(self) -> None:
        calls = {"resources": 0}

        def counting(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token/accessible-resources":
                calls["resources"] += 1
            return scripted(request)

        jira, jira_client = jira_over(counting)
        async with jira_client:
            _ = [i async for i in jira.list_since(None)]
            _ = await jira.fetch("issue:ENG-7")
        assert calls["resources"] == 1

        confluence, confluence_client = confluence_over(counting)
        async with confluence_client:
            _ = [i async for i in confluence.list_since(None)]
            _ = await confluence.fetch("page:98305")
        assert calls["resources"] == 2

    async def test_a_token_reaching_no_site_is_permanent(self) -> None:
        def siteless(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/oauth/token/accessible-resources"
            return httpx.Response(200, json=[])

        connector, client = jira_over(siteless)
        async with client:
            with pytest.raises(ProviderApiError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert excinfo.value.transient is False
        assert "no Atlassian site" in str(excinfo.value)

    def test_both_connectors_satisfy_the_connector_protocol(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(scripted))
        assert isinstance(JiraConnector(JIRA_CONTEXT, StaticToken(), client), Connector)
        assert isinstance(ConfluenceConnector(CONFLUENCE_CONTEXT, StaticToken(), client), Connector)


class TestFailureTaxonomy:
    async def test_a_rate_limit_is_transient_and_carries_retry_after(self) -> None:
        def limited(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token/accessible-resources":
                return httpx.Response(200, json=RESOURCES)
            return httpx.Response(429, headers={"Retry-After": "30"}, json={})

        connector, client = jira_over(limited)
        async with client:
            with pytest.raises(ProviderApiError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert excinfo.value.transient is True
        assert excinfo.value.retry_after == 30.0

    async def test_a_dead_token_is_an_auth_error_not_a_retry(self) -> None:
        def unauthorized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Unauthorized"})

        connector, client = confluence_over(unauthorized)
        async with client:
            with pytest.raises(ProviderAuthError):
                _ = [i async for i in connector.list_since(None)]

    async def test_no_error_text_ever_carries_the_token(self) -> None:
        def unauthorized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Unauthorized"})

        connector, client = jira_over(unauthorized)
        async with client:
            with pytest.raises(ProviderAuthError) as excinfo:
                _ = [i async for i in connector.list_since(None)]
        assert "atl-token" not in json.dumps(str(excinfo.value))
