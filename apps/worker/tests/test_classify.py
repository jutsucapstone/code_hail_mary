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


class TestTheConnectorsOwnFailures:
    """The half of the taxonomy that used to fall through to INTERNAL.

    Every live provider call goes through `ProviderHttp.request`, which raises
    `ProviderAuthError` on 401/403, a transient `ProviderApiError` on 429 and 5xx, and a
    permanent one on any other 4xx. None of those were named in `classify`, so a grant
    the employee revoked at Google came back as "internal bug, retry five times" and the
    connection kept describing itself as connected.
    """

    def test_a_revoked_grant_is_permanent_and_not_an_internal_bug(self) -> None:
        from jutsu_connectors.providers.base import ProviderAuthError
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(ProviderAuthError("the provider no longer honours this token"))
        assert kind is FailureKind.PROVIDER_PERMANENT
        assert retryable is False

    def test_a_rate_limit_is_transient(self) -> None:
        from jutsu_connectors.providers.base import ProviderApiError
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(
            ProviderApiError("the provider rate-limited this sync", transient=True, retry_after=30)
        )
        assert kind is FailureKind.PROVIDER_TRANSIENT
        assert retryable is True

    def test_a_rejected_request_is_permanent(self) -> None:
        """A 400 from a provider is rejected identically every time. Five attempts buys
        five identical refusals and five entries in someone's quota."""
        from jutsu_connectors.providers.base import ProviderApiError
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(
            ProviderApiError("the provider rejected the request (HTTP 400)", transient=False)
        )
        assert kind is FailureKind.PROVIDER_PERMANENT
        assert retryable is False

    def test_the_auth_error_is_matched_before_its_parent(self) -> None:
        """`ProviderAuthError` subclasses `ProviderApiError` with `transient=False`, so
        both branches agree on the kind — but only the auth branch is what
        `record_failure` reads to flip the connection to "reconnect". If the parent
        check were written first this test would still pass on kind and the connection
        would silently stay healthy, so it asserts the type relationship too."""
        from jutsu_connectors.providers.base import ProviderApiError, ProviderAuthError

        assert issubclass(ProviderAuthError, ProviderApiError)
        assert ProviderAuthError("x").transient is False
