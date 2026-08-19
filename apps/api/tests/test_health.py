"""Gateway contract tests.

Cover the three things S0 actually promises: liveness, honest readiness, and the single
error envelope with a propagated request id.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jutsu_api.main import REQUEST_ID_HEADER, create_app
from jutsu_core import AclDenied, NotFound, ValidationFailed


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


class TestHealth:
    def test_healthz_is_ok(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_readyz_is_degraded_until_dependencies_exist(self, client: TestClient) -> None:
        """An unconditional 200 would make the Cloud Run gate meaningless at S1."""
        body = client.get("/readyz").json()
        assert body["status"] == "degraded"
        assert body["checks"]["postgres"] == "not_configured"


class TestRequestId:
    def test_response_always_carries_one(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.headers[REQUEST_ID_HEADER]
        assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]

    def test_inbound_id_is_preserved(self, client: TestClient) -> None:
        """A trace must survive across hops rather than restarting at each service."""
        response = client.get("/healthz", headers={REQUEST_ID_HEADER: "trace-abc"})
        assert response.headers[REQUEST_ID_HEADER] == "trace-abc"
        assert response.json()["request_id"] == "trace-abc"

    def test_ids_differ_between_requests(self, client: TestClient) -> None:
        first = client.get("/healthz").headers[REQUEST_ID_HEADER]
        second = client.get("/healthz").headers[REQUEST_ID_HEADER]
        assert first != second


class TestErrorEnvelope:
    @staticmethod
    def _app_raising(exc: Exception) -> TestClient:
        app: FastAPI = create_app()

        @app.get("/boom")
        async def boom() -> None:
            raise exc

        return TestClient(app, raise_server_exceptions=False)

    def test_shape_is_uniform(self) -> None:
        client = self._app_raising(ValidationFailed("bad field", details={"field": "topic"}))
        response = client.get("/boom")
        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "validation_failed"
        assert body["error"]["details"] == {"field": "topic"}
        assert body["request_id"]

    def test_acl_denial_is_indistinguishable_from_not_found(self) -> None:
        """§4.5 — leaking existence through a status code is the same leak as
        returning the content."""
        denied = self._app_raising(AclDenied("not found")).get("/boom")
        missing = self._app_raising(NotFound("not found")).get("/boom")

        assert denied.status_code == missing.status_code == 404
        assert denied.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found"
