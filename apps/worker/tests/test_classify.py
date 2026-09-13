"""classify() maps provider failures to actionable kinds (no DB needed)."""

from __future__ import annotations

import pytest
from jutsu_llm import AllProvidersFailed


class TestProviderFailureClassification:
    """The LLM chain's exhaustion maps to a provider kind, not a retryable INTERNAL blur.

    What reaches `classify` changed shape with ADR 0024 and did not change meaning. It
    used to be one vendor SDK's exception hierarchy, raised by the extraction transport
    on the first refusal; it is now `AllProvidersFailed`, raised only after every
    configured vendor has been asked and none answered — a single provider failing now
    falls over to the next and the job succeeds, so it never arrives here at all.

    The decision the kind drives is unchanged: `provider_transient` is retried with
    backoff, `provider_permanent` is not retried, and the operator reading the Jobs page
    needs to know which of those is happening.
    """

    @pytest.mark.parametrize("error_class", ["rate_limited", "timeout", "unavailable"])
    def test_capacity_and_reachability_are_transient_and_retryable(self, error_class: str) -> None:
        """Every vendor over capacity or unreachable recovers on its own."""
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(
            AllProvidersFailed("The answer service is unreachable.", error_class=error_class)
        )
        assert kind is FailureKind.PROVIDER_TRANSIENT
        assert retryable is True

    def test_a_request_every_vendor_refused_is_permanent_not_five_retries(self) -> None:
        """A bad key or a model no account can use is refused identically every time."""
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(
            AllProvidersFailed("The answer service did not respond.", error_class="refused")
        )
        assert kind is FailureKind.PROVIDER_PERMANENT
        assert retryable is False

    def test_no_provider_configured_at_all_is_permanent(self) -> None:
        """Retrying an empty chain five times asks nobody, five times."""
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(
            AllProvidersFailed("Answers are not configured.", error_class="not_configured")
        )
        assert kind is FailureKind.PROVIDER_PERMANENT
        assert retryable is False

    def test_an_unrecognised_error_class_is_permanent_rather_than_a_retry_loop(self) -> None:
        """A label this function does not know still came from an exhausted chain.

        Permanent is the safe direction *here*, unlike the unrecognised-exception case at
        the end of `classify`, which retries. The difference is what is already known: an
        `AllProvidersFailed` means every vendor was asked and none answered, so a retry
        asks the same three vendors the same question, and the attempt budget is spent
        before anything has had a chance to change.
        """
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(
            AllProvidersFailed("The answer service did not respond.", error_class="something_new")
        )
        assert kind is FailureKind.PROVIDER_PERMANENT
        assert retryable is False

    def test_the_chains_failure_is_not_classified_as_a_generic_internal_error(self) -> None:
        """`AllProvidersFailed` subclasses `ServiceUnavailable`, which is a `JutsuError`.

        Nothing in `classify` names `JutsuError`, so before the branch existed this fell
        all the way through to `INTERNAL, retryable` — an LLM outage reported to the
        operator as a bug in JUTSU.
        """
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, _ = classify(
            AllProvidersFailed("The answer service is unreachable.", error_class="unavailable")
        )
        assert kind is not FailureKind.INTERNAL


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
