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

    def test_readyz_probes_postgres_for_real(self, client: TestClient) -> None:
        """`ok` must mean a connection answered, `failed` that one did not.

        The check used to be a hardcoded literal that reported `degraded`
        unconditionally, which made the Cloud Run gate a decoration. Now the answer is
        whatever `jutsu_db.engine.ping()` actually observed — so this test accepts
        either verdict but refuses the one value that can no longer occur: a claim that
        Postgres is not configured at all.
        """
        body = client.get("/readyz").json()
        assert body["checks"]["postgres"] in ("ok", "failed")
        assert body["checks"]["neo4j"] == "not_configured"
        # Readiness follows the probe: a reachable database is ready even while Neo4j
        # is honestly not configured; an unreachable one is degraded.
        expected = "ready" if body["checks"]["postgres"] == "ok" else "degraded"
        assert body["status"] == expected


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


class TestUnhandledErrors:
    """Nothing escapes the envelope, including what nobody anticipated.

    Found by running the built container with no database attached:
    `/v1/orgs/register` answered a plain-text "Internal Server Error" while every other
    failure on the service answers as JSON. A caller then has no `request_id` to quote
    and the request cannot be found in the logs.
    """

    @staticmethod
    def _app_raising(exc: Exception) -> TestClient:
        app: FastAPI = create_app()

        @app.get("/boom")
        async def boom() -> None:
            raise exc

        return TestClient(app, raise_server_exceptions=False)

    def test_an_unexpected_exception_still_returns_the_envelope(self) -> None:
        response = self._app_raising(RuntimeError("connection string in here")).get("/boom")

        assert response.status_code == 500
        body = response.json()
        assert body["error"]["code"] == "internal_error"
        assert body["request_id"]

    def test_the_exception_text_never_reaches_the_caller(self) -> None:
        """§4.9. The message a bug raises is not vetted for what it carries.

        `DATABASE_URL is not set` is the harmless version; the same path can raise with a
        DSN, a host name or a stack trace naming every internal package.
        """
        secret = "postgresql://user:hunter2@internal-host/db"
        response = self._app_raising(RuntimeError(secret)).get("/boom")

        assert secret not in response.text
        assert "hunter2" not in response.text
        assert "Traceback" not in response.text

    def test_the_request_id_is_the_one_the_caller_sent(self) -> None:
        """So the id in a bug report matches the id in the logs."""
        client = self._app_raising(RuntimeError("boom"))
        response = client.get("/boom", headers={"x-request-id": "trace-me"})

        assert response.json()["request_id"] == "trace-me"
        assert response.headers["x-request-id"] == "trace-me"
