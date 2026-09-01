"""classify() maps provider SDK errors to actionable kinds (no DB needed)."""

from __future__ import annotations


class TestProviderFailureClassification:
    """Anthropic SDK errors map to provider kinds, not to a retryable INTERNAL blur."""

    def _status_error(self, status_code: int) -> object:
        import anthropic
        import httpx2 as httpx

        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(status_code, request=request)
        cls_by_status = {
            401: anthropic.AuthenticationError,
            404: anthropic.NotFoundError,
            429: anthropic.RateLimitError,
            500: anthropic.InternalServerError,
        }
        return cls_by_status[status_code](f"status {status_code}", response=response, body=None)

    def test_rate_limit_is_transient_and_retryable(self) -> None:
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(self._status_error(429))  # type: ignore[arg-type]
        assert kind is FailureKind.PROVIDER_TRANSIENT
        assert retryable is True

    def test_a_server_error_is_transient_and_retryable(self) -> None:
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(self._status_error(500))  # type: ignore[arg-type]
        assert kind is FailureKind.PROVIDER_TRANSIENT
        assert retryable is True

    def test_bad_credentials_are_permanent_not_five_retries(self) -> None:
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(self._status_error(401))  # type: ignore[arg-type]
        assert kind is FailureKind.PROVIDER_PERMANENT
        assert retryable is False

    def test_a_missing_model_is_permanent(self) -> None:
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(self._status_error(404))  # type: ignore[arg-type]
        assert kind is FailureKind.PROVIDER_PERMANENT
        assert retryable is False

    def test_an_unreachable_provider_is_transient(self) -> None:
        import anthropic
        import httpx2 as httpx
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        error = anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        kind, retryable = classify(error)
        assert kind is FailureKind.PROVIDER_TRANSIENT
        assert retryable is True
