"""The Vertex transport: request shape, error classification, and what never gets logged.

No network. `httpx.MockTransport` answers in-process, which is what makes it possible to
assert on the exact bytes that would have gone out — the `task_type` on the wire is the
§9.3 hazard, and asserting it anywhere other than the outgoing request is asserting the
test's own assumptions.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from jutsu_retrieval.client import VertexTransport, classify_status
from jutsu_retrieval.errors import PermanentEmbeddingError, TransientEmbeddingError
from retrieval_support import RECORDED_RESPONSE, settings


class StubCredentials:
    """Stands in for ADC. `valid` is True so no refresh is attempted."""

    valid = True
    token = "stub-token-not-a-real-credential"


def transport_with(handler: object) -> VertexTransport:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return VertexTransport(settings(), client=client, credentials=StubCredentials())


class TestStatusClassification:
    @pytest.mark.parametrize("status", [200, 201, 299])
    def test_success_is_not_an_error(self, status: int) -> None:
        assert classify_status(status) is None

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_retryable_statuses(self, status: int) -> None:
        """429 is a per-minute quota that clears; 5xx is the provider having a moment.

        Observed live: 429 `online_prediction_requests_per_base_model` after roughly
        eight rapid requests.
        """
        error = classify_status(status)
        assert isinstance(error, TransientEmbeddingError)
        assert error.status == status

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
    def test_non_retryable_statuses(self, status: int) -> None:
        """A 400 is rejected identically every time. Observed live for an unknown
        `task_type` and for empty content."""
        error = classify_status(status)
        assert isinstance(error, PermanentEmbeddingError)
        assert error.status == status


class TestRequestShape:
    async def test_the_task_type_reaches_the_wire(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=RECORDED_RESPONSE)

        client = transport_with(handler)
        await client.predict(
            instances=[{"content": "text", "task_type": "RETRIEVAL_QUERY"}],
            parameters={"outputDimensionality": 768},
        )
        await client.aclose()

        instances = seen["instances"]
        assert isinstance(instances, list)
        assert instances[0]["task_type"] == "RETRIEVAL_QUERY"
        assert seen["parameters"] == {"outputDimensionality": 768}

    async def test_the_bearer_token_is_attached(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json=RECORDED_RESPONSE)

        client = transport_with(handler)
        await client.predict(instances=[], parameters={})
        await client.aclose()
        assert seen["auth"].startswith("Bearer ")

    async def test_the_url_is_regional(self) -> None:
        """§20: Vertex AI is regional here, and the region is a residency decision."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=RECORDED_RESPONSE)

        client = transport_with(handler)
        await client.predict(instances=[], parameters={})
        await client.aclose()

        assert "asia-south1-aiplatform.googleapis.com" in seen["url"]
        assert "publishers/google/models/gemini-embedding-001:predict" in seen["url"]


class TestErrorHandling:
    async def test_a_429_becomes_transient(self) -> None:
        client = transport_with(lambda request: httpx.Response(429, json={"error": {}}))
        with pytest.raises(TransientEmbeddingError) as caught:
            await client.predict(instances=[], parameters={})
        await client.aclose()
        assert caught.value.status == 429

    async def test_a_400_becomes_permanent(self) -> None:
        client = transport_with(lambda request: httpx.Response(400, json={"error": {}}))
        with pytest.raises(PermanentEmbeddingError):
            await client.predict(instances=[], parameters={})
        await client.aclose()

    async def test_a_timeout_becomes_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        client = transport_with(handler)
        with pytest.raises(TransientEmbeddingError, match="timed out"):
            await client.predict(instances=[], parameters={})
        await client.aclose()

    async def test_a_connection_failure_becomes_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        client = transport_with(handler)
        with pytest.raises(TransientEmbeddingError):
            await client.predict(instances=[], parameters={})
        await client.aclose()


class TestNoLeakage:
    async def test_an_error_never_carries_the_provider_body(self) -> None:
        """A provider error body quotes the input that caused it — which is chunk text.

        Masked chunk text is not public: ADR 0005 records that there is no PERSON
        detector, so masked text still contains names.
        """
        secret = "CONFIDENTIAL-PROJECT-FALCON"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"message": f"bad input: {secret}"}})

        client = transport_with(handler)
        with pytest.raises(PermanentEmbeddingError) as caught:
            await client.predict(
                instances=[{"content": secret, "task_type": "RETRIEVAL_DOCUMENT"}],
                parameters={},
            )
        await client.aclose()

        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)

    async def test_nothing_is_logged_during_a_request(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret = "CONFIDENTIAL-PROJECT-FALCON"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=RECORDED_RESPONSE)

        client = transport_with(handler)
        with caplog.at_level(logging.DEBUG, logger="jutsu"):
            await client.predict(
                instances=[{"content": secret, "task_type": "RETRIEVAL_DOCUMENT"}],
                parameters={"outputDimensionality": 768},
            )
        await client.aclose()

        for record in caplog.records:
            message = record.getMessage()
            assert secret not in message
            assert "stub-token" not in message

    def test_settings_repr_carries_no_credential(self) -> None:
        rendered = repr(settings())
        assert "test-project" in rendered
        assert "token" not in rendered.lower()
