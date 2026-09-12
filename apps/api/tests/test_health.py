"""Gateway contract tests.

Cover the three things S0 actually promises: liveness, honest readiness, and the single
error envelope with a propagated request id.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jutsu_api.main import REQUEST_ID_HEADER, create_app
from jutsu_core import AclDenied, NotFound, ValidationFailed
from jutsu_db.engine import dispose_engine
from jutsu_graph.health import GraphStatus


async def _no_graph(**_: object) -> GraphStatus:
    """The graph probe, for tests that are not about the graph."""
    return GraphStatus.NOT_CONFIGURED


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The app, with `/readyz`'s graph probe scripted.

    **Nothing in this file may open a real Neo4j connection**, and the reason is the
    shape of the driver rather than a preference. `jutsu_graph` caches one driver per
    process, bound to whichever event loop created it, and a `TestClient` runs on a
    private loop that is closed the moment the test ends. A pool created here therefore
    cannot be used by the next test *or closed by it* — `AsyncDriver.close()` raises when
    its sockets belong to a dead loop, so even the teardown that tried to clean up became
    an error with no test of its own in the traceback.

    The endpoint's graph contract — which word appears for which `GraphStatus` — is
    asserted in `TestGraphReadiness` with a scripted probe, and the probe's own behaviour
    against a live store belongs in `packages/graph`, where a running Neo4j is a declared
    dependency of the suite. Neither needs a driver here.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("jutsu_api.main.graph_probe", _no_graph)
        yield TestClient(create_app())
    # Postgres is still really pinged, and its engine is cached per process in the same
    # way. SQLAlchemy's dispose tolerates being called from another loop — it terminates
    # rather than closing gracefully — which is why this one line has always been enough.
    asyncio.run(dispose_engine())


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
        # Readiness follows the **required** dependency alone. Neo4j is optional and
        # cannot move this verdict — `TestGraphReadiness` is where that is proven.
        expected = "ready" if body["checks"]["postgres"] == "ok" else "degraded"
        assert body["status"] == expected


class TestGraphReadiness:
    """Neo4j is reported honestly and is never allowed to make JUTSU unready (ADR 0022).

    The failure this guards against is a redeploy rolling back over an optional store: if
    an unreachable graph made `/readyz` say `degraded`, Cloud Run's own check would fail
    the deploy of a service whose retrieval was working perfectly the whole time.

    The probe is monkeypatched rather than pointed at a real database, because the states
    that matter are the ones a healthy development machine cannot produce on demand — and
    because `jutsu_graph.health.probe` caches its answer, which would otherwise leak
    between tests.
    """

    @staticmethod
    def _client(
        monkeypatch: pytest.MonkeyPatch, status: GraphStatus, *, enabled: bool
    ) -> TestClient:
        async def scripted(**_: object) -> GraphStatus:
            return status

        monkeypatch.setattr("jutsu_api.main.graph_probe", scripted)
        monkeypatch.setenv("GRAPHRAG_ENABLED", "true" if enabled else "false")
        return TestClient(create_app())

    def test_an_unconfigured_graph_is_reported_and_is_not_an_outage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(monkeypatch, GraphStatus.NOT_CONFIGURED, enabled=False)

        body = client.get("/readyz").json()

        assert body["checks"]["neo4j"] == "not_configured"
        assert body["checks"]["graph_rag"] == "disabled"
        assert body["status"] == "ready"
        asyncio.run(dispose_engine())

    def test_a_configured_and_answering_graph_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._client(monkeypatch, GraphStatus.OK, enabled=True)

        body = client.get("/readyz").json()

        assert body["checks"]["neo4j"] == "ok"
        assert body["checks"]["graph_rag"] == "ok"
        asyncio.run(dispose_engine())

    def test_an_unreachable_graph_is_degraded_and_still_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole contract in one assertion: the operator is told, and the platform is
        # not. Answers are still correct — they are being built from pgvector alone.
        client = self._client(monkeypatch, GraphStatus.DEGRADED, enabled=True)

        body = client.get("/readyz").json()

        assert body["checks"]["neo4j"] == "degraded"
        assert body["checks"]["graph_rag"] == "degraded"
        assert body["status"] == "ready"
        asyncio.run(dispose_engine())

    def test_the_feature_reads_disabled_even_when_the_store_is_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two different operator problems: "the graph is down" and "the graph is up and
        # we are not using it". The store's line and the feature's line say so separately.
        client = self._client(monkeypatch, GraphStatus.OK, enabled=False)

        body = client.get("/readyz").json()

        assert body["checks"]["neo4j"] == "ok"
        assert body["checks"]["graph_rag"] == "disabled"
        asyncio.run(dispose_engine())

    def test_only_a_failed_check_makes_the_service_unready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`failed` is the only word that means an outage, and only Postgres says it.

        Asserted on the vocabulary rather than on an outcome: a future check reporting
        `degraded` for something required would pass every other test in this class while
        silently removing the gate.
        """
        client = self._client(monkeypatch, GraphStatus.DEGRADED, enabled=True)

        body = client.get("/readyz").json()

        assert "failed" not in {value for key, value in body["checks"].items() if key != "postgres"}
        asyncio.run(dispose_engine())


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


class TestTheRouterRefusesInTheOneEnvelope:
    """A path or a verb the router never matched is still a §15 error.

    Starlette answers `HTTPException` itself, before the application's own handlers see
    it, and its shape is `{"detail": "Not Found"}` — no `code`, no `details`, and no
    `request_id`. So the failure a client hits most often, a typo'd path or a wrong
    verb, was the one it could not parse with the code path it uses for every other
    error, and the one nobody could correlate to a log line. Verified against production
    before it was fixed: `GET /v1/nope` returned exactly that.
    """

    def test_an_unmatched_path_is_the_envelope_with_a_request_id(self, client: TestClient) -> None:
        response = client.get("/v1/nope")

        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "not_found"
        assert body["error"]["details"] == {}
        assert body["request_id"] == response.headers[REQUEST_ID_HEADER]
        assert "detail" not in body

    def test_a_wrong_method_keeps_the_allow_header(self, client: TestClient) -> None:
        """The envelope must not cost the caller the one header that says what to do
        instead — a 405 without `Allow` is a refusal with no way forward."""
        response = client.delete("/readyz")

        assert response.status_code == 405
        assert response.json()["error"]["code"] == "method_not_allowed"
        assert "allow" in {name.lower() for name in response.headers}

    def test_it_never_forwards_starlettes_own_wording(self, client: TestClient) -> None:
        """`exc.detail` is Starlette's English for the status, and for an
        `HTTPException` raised elsewhere it could carry text this handler has not
        vetted. The sentence is written by us."""
        body = client.get("/v1/definitely-not-a-route").json()

        assert body["error"]["message"] == "That endpoint does not exist."
