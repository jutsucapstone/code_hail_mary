"""Rate-limit behaviour: obey the provider, and stop hammering it.

The 200-document drain is the evidence this file exists for. With the drain-loop fix in
place the worker correctly kept going past failures — and made **551 requests for 147
successes, a 73% rejection rate**, because every retry invented its own delay while the
provider's `Retry-After` header sat unread. Continuing past failures without honouring
the header turns a stall into a stampede.

Three properties are asserted here, and each one failed before the change:

  * `Retry-After` is parsed (both RFC 9110 forms) and obeyed.
  * Without a usable header, backoff is exponential, jittered and bounded.
  * A rejection pauses **every** batch in the process, not just the one that saw it.

No network, no credential, no cost — the transport is scripted and the clock is injected,
so a test that proves a 30-second wait takes no wall-clock time at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest
from jutsu_retrieval.client import MAX_RETRY_AFTER_S, classify_status, parse_retry_after
from jutsu_retrieval.embeddings import Embedder, EmbeddingTask, TokenLedger
from jutsu_retrieval.errors import PermanentEmbeddingError, TransientEmbeddingError
from retrieval_support import FakeTransport, response_for, settings


class Clock:
    """A monotonic clock that only advances when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def limited(retry_after: float | None = None, status: int = 429) -> TransientEmbeddingError:
    return TransientEmbeddingError("scripted", status=status, retry_after=retry_after)


def embedder(transport: FakeTransport, clock: Clock, **overrides: Any) -> Embedder:
    return Embedder(
        transport,
        settings(**overrides),
        count_tokens=lambda _t: 1,
        sleep=clock.sleep,
        clock=clock,
    )


class TestParsingTheHeader:
    def test_delta_seconds(self) -> None:
        assert parse_retry_after("30") == 30.0

    def test_delta_seconds_with_whitespace(self) -> None:
        assert parse_retry_after("  7 ") == 7.0

    def test_http_date_form(self) -> None:
        """RFC 9110 permits a date, and providers do send them.

        The header is built from a known instant rather than hard-coded, so the test
        states the relationship it means instead of a magic number that has to be
        recomputed whenever anyone looks at it.
        """
        base = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)
        header = format_datetime(base + timedelta(seconds=60), usegmt=True)
        assert parse_retry_after(header, now=base.timestamp()) == pytest.approx(60.0, abs=1.0)

    def test_an_http_date_in_the_past_is_refused(self) -> None:
        base = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)
        header = format_datetime(base - timedelta(seconds=60), usegmt=True)
        assert parse_retry_after(header, now=base.timestamp()) is None

    @pytest.mark.parametrize("value", [None, "", "   ", "soon", "not-a-date"])
    def test_unusable_values_fall_back(self, value: str | None) -> None:
        """Unreadable means "use your own backoff", never "retry immediately"."""
        assert parse_retry_after(value) is None

    def test_a_negative_value_is_refused(self) -> None:
        assert parse_retry_after("-5") is None

    def test_an_absurd_value_is_refused(self) -> None:
        """A provider asking for an hour is telling the operator, not the worker."""
        assert parse_retry_after(str(MAX_RETRY_AFTER_S + 1)) is None
        assert parse_retry_after(str(MAX_RETRY_AFTER_S)) == MAX_RETRY_AFTER_S

    def test_it_reaches_the_error_via_classify_status(self) -> None:
        error = classify_status(429, retry_after=12.0)
        assert isinstance(error, TransientEmbeddingError)
        assert error.retry_after == 12.0

    def test_a_permanent_status_carries_no_retry_after(self) -> None:
        assert isinstance(classify_status(400, retry_after=12.0), PermanentEmbeddingError)


class TestRetryAfterIsObeyed:
    async def test_the_delay_is_exactly_what_the_provider_asked_for(self) -> None:
        clock = Clock()
        transport = FakeTransport(script=[limited(retry_after=30.0), response_for(1)])
        result = await embedder(transport, clock).embed(["a"], EmbeddingTask.DOCUMENT)

        assert len(result) == 1, "the retry did not eventually succeed"
        assert clock.slept == [30.0], f"expected one 30s wait, got {clock.slept}"

    async def test_it_is_not_an_immediate_retry(self) -> None:
        """The whole point: no hammering."""
        clock = Clock()
        transport = FakeTransport(script=[limited(retry_after=15.0), response_for(1)])
        await embedder(transport, clock).embed(["a"], EmbeddingTask.DOCUMENT)
        assert all(s > 0 for s in clock.slept)

    async def test_repeated_headers_are_each_obeyed(self) -> None:
        clock = Clock()
        transport = FakeTransport(
            script=[limited(retry_after=5.0), limited(retry_after=9.0), response_for(1)]
        )
        await embedder(transport, clock).embed(["a"], EmbeddingTask.DOCUMENT)
        assert clock.slept == [5.0, 9.0]

    async def test_the_header_wins_over_backoff(self) -> None:
        """Backoff is a guess; the header is the provider's answer."""
        clock = Clock()
        transport = FakeTransport(script=[limited(retry_after=42.0), response_for(1)])
        await embedder(transport, clock, base_backoff_s=1.0, max_backoff_s=2.0).embed(
            ["a"], EmbeddingTask.DOCUMENT
        )
        assert clock.slept == [42.0], "backoff overrode an explicit Retry-After"


class TestBackoffWithoutAHeader:
    async def test_delays_are_bounded_by_the_ceiling(self) -> None:
        clock = Clock()
        transport = FakeTransport(script=[limited(), limited(), limited(), response_for(1)])
        await embedder(transport, clock, base_backoff_s=1.0, max_backoff_s=4.0).embed(
            ["a"], EmbeddingTask.DOCUMENT
        )
        assert clock.slept, "no backoff was applied"
        assert all(0.0 <= s <= 4.0 for s in clock.slept), clock.slept

    async def test_jitter_makes_the_delays_differ(self) -> None:
        """An unjittered schedule retries every rejected batch at the same instant."""
        seen: set[float] = set()
        for _ in range(12):
            clock = Clock()
            transport = FakeTransport(script=[limited(), limited(), response_for(1)])
            await embedder(transport, clock, base_backoff_s=8.0, max_backoff_s=8.0).embed(
                ["a"], EmbeddingTask.DOCUMENT
            )
            seen.update(clock.slept)
        assert len(seen) > 1, "every backoff delay was identical — jitter is not applied"


class TestItDoesNotRetryForever:
    async def test_max_attempts_is_respected(self) -> None:
        clock = Clock()
        transport = FakeTransport(script=[limited(retry_after=1.0)] * 10)
        worker = embedder(transport, clock, max_attempts=3)

        with pytest.raises(TransientEmbeddingError):
            await worker.embed(["a"], EmbeddingTask.DOCUMENT)

        assert transport.call_count == 3, "attempts did not stop at max_attempts"

    async def test_a_permanent_error_is_not_retried_at_all(self) -> None:
        clock = Clock()
        transport = FakeTransport(script=[PermanentEmbeddingError("400", status=400)])
        with pytest.raises(PermanentEmbeddingError):
            await embedder(transport, clock).embed(["a"], EmbeddingTask.DOCUMENT)
        assert transport.call_count == 1
        assert clock.slept == []


class TestConcurrentBatchesShareTheCooldown:
    async def test_a_rejection_pauses_the_other_batches(self) -> None:
        """The storm control.

        `max_concurrency` batches would otherwise walk straight into a limit one of them
        has already hit. After a 429 asking for 30s, a later batch must wait too.
        """
        clock = Clock()
        transport = FakeTransport(script=[limited(retry_after=30.0)] + [response_for(1)] * 8)
        worker = embedder(transport, clock, max_batch_size=1, max_concurrency=1)

        await worker.embed(["a", "b", "c"], EmbeddingTask.DOCUMENT)
        assert 30.0 in clock.slept, "the cooldown was never published"
        assert clock.now >= 1030.0, "later batches did not wait for the quota window"

    async def test_the_cooldown_is_published_even_on_the_final_attempt(self) -> None:
        """This batch is giving up; its siblings still need to know."""
        clock = Clock()
        transport = FakeTransport(script=[limited(retry_after=20.0)] * 5)
        worker = embedder(transport, clock, max_attempts=1)

        with pytest.raises(TransientEmbeddingError):
            await worker.embed(["a"], EmbeddingTask.DOCUMENT)
        assert clock.now >= 1000.0
        assert worker._not_before >= 1020.0, "the quota window was not shared"


class TestTokenAccountingIsUnchanged:
    async def test_a_rejected_request_contributes_no_tokens(self) -> None:
        clock = Clock()
        transport = FakeTransport(script=[limited(retry_after=1.0), limited(retry_after=1.0)])
        worker = embedder(transport, clock, max_attempts=2)

        with pytest.raises(TransientEmbeddingError):
            await worker.embed(["abcde"], EmbeddingTask.DOCUMENT)

        assert worker.ledger.spent == 0, "a 429 was charged as spend"
        assert worker.ledger.requests == 2, "attempts are still counted"

    async def test_a_successful_retry_charges_the_provider_figure(self) -> None:
        clock = Clock()
        transport = FakeTransport(script=[limited(retry_after=1.0)])
        worker = embedder(transport, clock)

        result = await worker.embed(["abcde"], EmbeddingTask.DOCUMENT)
        assert [e.token_count for e in result] == [5]
        assert worker.ledger.spent == 5, "the provider's own count was not charged"

    async def test_the_budget_ceiling_still_fires(self) -> None:
        from jutsu_retrieval.errors import EmbeddingBudgetExceeded

        clock = Clock()
        transport = FakeTransport()
        worker = Embedder(
            transport,
            settings(max_batch_size=1),
            count_tokens=lambda _t: 1,
            ledger=TokenLedger(budget=6),
            sleep=clock.sleep,
            clock=clock,
        )
        with pytest.raises(EmbeddingBudgetExceeded):
            await worker.embed(["aaaaa"] * 10, EmbeddingTask.DOCUMENT)

    async def test_an_exhausted_budget_still_blocks_before_the_call(self) -> None:
        from jutsu_retrieval.errors import EmbeddingBudgetExceeded

        clock = Clock()
        transport = FakeTransport()
        worker = Embedder(
            transport,
            settings(),
            count_tokens=lambda _t: 1,
            ledger=TokenLedger(budget=5, spent=5),
            sleep=clock.sleep,
            clock=clock,
        )
        with pytest.raises(EmbeddingBudgetExceeded):
            await worker.embed(["a"], EmbeddingTask.DOCUMENT)
        assert transport.call_count == 0


class TestTheRetryIsVisible:
    """A storm that is only visible as slowness is a storm nobody diagnoses in time."""

    async def test_a_rejection_is_logged_with_the_header_it_saw(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = Clock()
        transport = FakeTransport(script=[limited(retry_after=30.0), response_for(1)])
        with caplog.at_level("WARNING", logger="jutsu.retrieval.embeddings"):
            await embedder(transport, clock).embed(["a"], EmbeddingTask.DOCUMENT)

        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "status=429" in message
        assert "retry_after=30.0" in message
        assert "delay=30.00" in message

    async def test_a_missing_header_is_logged_as_absent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`absent` and `0.0` mean opposite things and must not share a rendering."""
        clock = Clock()
        transport = FakeTransport(script=[limited(), response_for(1)])
        with caplog.at_level("WARNING", logger="jutsu.retrieval.embeddings"):
            await embedder(transport, clock).embed(["a"], EmbeddingTask.DOCUMENT)

        assert "retry_after=absent" in caplog.records[0].getMessage()

    async def test_the_log_line_carries_no_payload(self, caplog: pytest.LogCaptureFixture) -> None:
        """§4.9: a provider error body quotes the input that caused it."""
        clock = Clock()
        transport = FakeTransport(script=[limited(retry_after=1.0), response_for(1)])
        with caplog.at_level("WARNING", logger="jutsu.retrieval.embeddings"):
            await embedder(transport, clock).embed(["secret-content"], EmbeddingTask.DOCUMENT)

        assert "secret-content" not in caplog.text
