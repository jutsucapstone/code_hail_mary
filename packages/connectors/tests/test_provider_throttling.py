"""A 403 that means "slow down" must not be read as "this grant is gone".

GitHub and Google both answer **403** when throttling. Classifying every 403 as
`ProviderAuthError` — which is permanent by construction — did two wrong things at once:
it failed the job for good instead of retrying under the ladder, and it fired
`mark_reauth_required`, telling the employee to reconnect an account that never stopped
working. A busy first sync of a large mailbox is exactly the case that triggers it.

So the distinction is tested against the shapes the real providers actually send, rather
than an invented one. Nothing here reaches a network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from jutsu_connectors.providers.base import (
    ProviderApiError,
    ProviderAuthError,
    ProviderHttp,
)


class StaticToken:
    async def access_token(self) -> str:
        return "token"


def http_over(handler: Any) -> ProviderHttp:
    return ProviderHttp(StaticToken(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def _get(handler: Any) -> None:
    await http_over(handler).request("GET", "https://provider.example/resource")


class TestAThrottleIsTransient:
    async def test_a_403_with_retry_after_is_retryable(self) -> None:
        """A provider that says when to come back has not revoked anything.

        A dead grant does not carry a `Retry-After`, because there is no later time at
        which it starts working again.
        """

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, headers={"Retry-After": "42"}, json={"message": "nope"})

        with pytest.raises(ProviderApiError) as raised:
            await _get(handler)

        assert not isinstance(raised.value, ProviderAuthError)
        assert raised.value.transient is True
        assert raised.value.retry_after == 42

    async def test_githubs_exhausted_rate_limit_is_retryable(self) -> None:
        # The real shape: 403, a zeroed remaining count, and a message that says so.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "5000"},
                json={
                    "message": "API rate limit exceeded for user ID 583231.",
                    "documentation_url": "https://docs.github.com/rest/overview/rate-limits",
                },
            )

        with pytest.raises(ProviderApiError) as raised:
            await _get(handler)

        assert not isinstance(raised.value, ProviderAuthError)
        assert raised.value.transient is True

    async def test_githubs_secondary_rate_limit_is_retryable(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={"message": "You have exceeded a secondary rate limit."},
            )

        with pytest.raises(ProviderApiError) as raised:
            await _get(handler)

        assert raised.value.transient is True

    @pytest.mark.parametrize(
        "reason", ["rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"]
    )
    async def test_googles_quota_reasons_are_retryable(self, reason: str) -> None:
        # Google's 403 body names the reason; all three mean "later", not "never".
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                content=json.dumps(
                    {
                        "error": {
                            "code": 403,
                            "message": "Rate Limit Exceeded",
                            "errors": [{"domain": "usageLimits", "reason": reason}],
                        }
                    }
                ),
                headers={"content-type": "application/json"},
            )

        with pytest.raises(ProviderApiError) as raised:
            await _get(handler)

        assert not isinstance(raised.value, ProviderAuthError)
        assert raised.value.transient is True


class TestARevocationIsStillPermanent:
    async def test_a_plain_403_is_still_a_dead_grant(self) -> None:
        """The case the original code was written for, and it must not regress.

        An employee who removed the app in their Google account, or an administrator who
        revoked it, produces a 403 with no throttling evidence — and the right answer is
        still "reconnect", not "retry".
        """

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "insufficient_scope"})

        with pytest.raises(ProviderAuthError) as raised:
            await _get(handler)

        assert raised.value.transient is False

    async def test_a_401_is_always_a_dead_grant(self) -> None:
        # Never throttling. A 401 carrying a Retry-After is still an expired token.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, headers={"Retry-After": "30"}, json={"message": "bad credentials"}
            )

        with pytest.raises(ProviderAuthError):
            await _get(handler)

    async def test_a_403_naming_a_scope_problem_is_not_a_throttle(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "errors": [{"domain": "global", "reason": "forbidden"}],
                    }
                },
            )

        with pytest.raises(ProviderAuthError):
            await _get(handler)


class TestTheOtherStatusesAreUnchanged:
    async def test_a_429_is_still_transient_and_carries_its_delay(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "7"})

        with pytest.raises(ProviderApiError) as raised:
            await _get(handler)

        assert raised.value.transient is True
        assert raised.value.retry_after == 7

    async def test_a_404_is_still_permanent_and_carries_its_status(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        with pytest.raises(ProviderApiError) as raised:
            await _get(handler)

        assert raised.value.transient is False
        assert raised.value.status == 404

    async def test_a_500_is_still_transient(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with pytest.raises(ProviderApiError) as raised:
            await _get(handler)

        assert raised.value.transient is True

    async def test_no_failure_message_carries_the_response_body(self) -> None:
        """Provider errors quote the request, and a request carries a token.

        A body echoed into `jobs.error` would reach the administrator's Jobs view and the
        exported audit trail.
        """

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403, json={"message": "API rate limit exceeded", "secret": "s3cr3t"}
            )

        with pytest.raises(ProviderApiError) as raised:
            await _get(handler)

        assert "s3cr3t" not in str(raised.value)
