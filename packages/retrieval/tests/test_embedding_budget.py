"""The token ceiling, end to end, and every way it could be silently exceeded.

§20 asks for a cost guardrail in code rather than in the billing console. The guardrail
had two holes, both of which passed the existing suite:

1. **Nothing populated it.** `get_embedding_settings()` read no budget variable, so
   `token_budget` was `None` on every production path and `TokenLedger` had no ceiling
   anywhere. The value in `.env` was `TOKEN_BUDGET_PER_REQUEST`, which is §13's *agent*
   ceiling and belongs to a layer that does not exist yet — wiring that in would have
   capped a 45k-document seed at 120 000 tokens while looking like a fix.
2. **It was only checked after spending.** `charge` runs when a batch returns, and the
   batches were dispatched by a `gather` that propagates the first exception without
   cancelling its siblings. Every queued batch therefore called the provider *after* the
   budget was gone, which costs real money and is invisible: the caller already has its
   exception.

So the tests here are adversarial rather than illustrative. Most of them assert
`transport.calls`, because a ceiling that raises correctly while still paying is the
failure that matters.
"""

from __future__ import annotations

import asyncio

import pytest
from jutsu_retrieval.config import MissingEmbeddingSettings, get_embedding_settings
from jutsu_retrieval.embeddings import Embedder, EmbeddingTask, TokenLedger
from jutsu_retrieval.errors import EmbeddingBudgetExceeded
from retrieval_support import FakeTransport, settings


@pytest.fixture
def vertex_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """The two variables `get_embedding_settings` refuses to default."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "jutsu-test")
    monkeypatch.setenv("VERTEX_LOCATION", "asia-south1")
    monkeypatch.delenv("EMBEDDING_TOKEN_BUDGET", raising=False)
    return monkeypatch


class TestTheConfigurationReachesTheLedger:
    """The first hole: a ceiling nothing ever set."""

    def test_the_budget_variable_is_read(self, vertex_env: pytest.MonkeyPatch) -> None:
        vertex_env.setenv("EMBEDDING_TOKEN_BUDGET", "2000000")
        assert get_embedding_settings().token_budget == 2_000_000

    def test_it_reaches_the_ledger_the_embedder_builds(
        self, vertex_env: pytest.MonkeyPatch
    ) -> None:
        """The whole point. Settings that hold a ceiling nobody enforces are decoration."""
        vertex_env.setenv("EMBEDDING_TOKEN_BUDGET", "1234")
        embedder = Embedder(FakeTransport(), get_embedding_settings())
        assert embedder.ledger.budget == 1234

    def test_the_agent_per_request_ceiling_is_not_the_embedding_ceiling(
        self, vertex_env: pytest.MonkeyPatch
    ) -> None:
        """The regression this fix must not become.

        `TOKEN_BUDGET_PER_REQUEST` is §13's bound on one agent question. Reading it here
        would stop a corpus-scale seed after a few dozen documents, and it would look
        like the guardrail working.
        """
        vertex_env.setenv("TOKEN_BUDGET_PER_REQUEST", "120000")
        assert get_embedding_settings().token_budget is None

    def test_absent_means_no_ceiling(self, vertex_env: pytest.MonkeyPatch) -> None:
        assert get_embedding_settings().token_budget is None

    def test_an_empty_value_means_no_ceiling_rather_than_a_crash(
        self, vertex_env: pytest.MonkeyPatch
    ) -> None:
        """`.env` files carry bare `KEY=` lines; `int("")` would raise on startup."""
        vertex_env.setenv("EMBEDDING_TOKEN_BUDGET", "   ")
        assert get_embedding_settings().token_budget is None

    def test_zero_is_refused_rather_than_read_as_unlimited(
        self, vertex_env: pytest.MonkeyPatch
    ) -> None:
        """A mistyped ceiling must not silently become no ceiling."""
        vertex_env.setenv("EMBEDDING_TOKEN_BUDGET", "0")
        with pytest.raises(MissingEmbeddingSettings, match="not a ceiling"):
            get_embedding_settings()

    def test_a_negative_ceiling_is_refused(self, vertex_env: pytest.MonkeyPatch) -> None:
        vertex_env.setenv("EMBEDDING_TOKEN_BUDGET", "-1")
        with pytest.raises(MissingEmbeddingSettings, match="not a ceiling"):
            get_embedding_settings()

    def test_a_non_integer_is_refused(self, vertex_env: pytest.MonkeyPatch) -> None:
        vertex_env.setenv("EMBEDDING_TOKEN_BUDGET", "lots")
        with pytest.raises(MissingEmbeddingSettings, match="not an integer"):
            get_embedding_settings()


class TestNothingIsSpentPastTheCeiling:
    """The second hole: raising correctly while continuing to pay."""

    async def test_an_already_exhausted_ledger_makes_no_call_at_all(self) -> None:
        transport = FakeTransport()
        embedder = Embedder(
            transport,
            settings(max_batch_size=1),
            count_tokens=lambda t: 1,
            ledger=TokenLedger(budget=10, spent=10),
        )
        with pytest.raises(EmbeddingBudgetExceeded):
            await embedder.embed(["aaa", "bbb"], EmbeddingTask.DOCUMENT)

        assert transport.call_count == 0, "the provider was called with no budget left"

    async def test_queued_batches_do_not_call_after_the_budget_is_gone(self) -> None:
        """Twenty batches, a ceiling reached at the second, serial dispatch.

        Before the fix `gather` left the remaining eighteen running: each acquired the
        semaphore and called the provider after the ledger had already raised.
        """
        transport = FakeTransport()
        embedder = Embedder(
            transport,
            settings(max_batch_size=1, max_concurrency=1),
            count_tokens=lambda t: 1,
            # Each fake response bills `len(content)` = 5 tokens per instance.
            ledger=TokenLedger(budget=8),
        )
        with pytest.raises(EmbeddingBudgetExceeded):
            await embedder.embed(["aaaaa"] * 20, EmbeddingTask.DOCUMENT)

        # Two calls: the first spends 5 of 8, the second crosses to 10 and raises. The
        # remaining eighteen must never reach the transport.
        assert transport.call_count == 2, (
            f"{transport.call_count} provider calls for a budget that ran out after 2"
        )

    async def test_no_call_is_made_once_the_ledger_reports_exhausted(self) -> None:
        """Stated as the invariant rather than as a count.

        Whatever the batching, the number of calls may never exceed the number that had
        started before `exhausted` first became true.
        """
        transport = FakeTransport()
        ledger = TokenLedger(budget=12)
        embedder = Embedder(
            transport,
            settings(max_batch_size=2, max_concurrency=1),
            count_tokens=lambda t: 1,
            ledger=ledger,
        )
        with pytest.raises(EmbeddingBudgetExceeded):
            await embedder.embed(["aaaaa"] * 30, EmbeddingTask.DOCUMENT)

        assert ledger.exhausted
        # Every call bills 10 tokens (2 instances x 5). Two calls reach 20, over 12.
        assert transport.call_count == 2

    async def test_concurrency_cannot_widen_the_overrun_without_bound(self) -> None:
        """With concurrency N, at most N requests can be in flight when it trips.

        That is the honest limit of a concurrent batcher: work already dispatched cannot
        be un-dispatched. What must not happen is the queue draining past it.
        """
        transport = FakeTransport()
        embedder = Embedder(
            transport,
            settings(max_batch_size=1, max_concurrency=2),
            count_tokens=lambda t: 1,
            ledger=TokenLedger(budget=1),
        )
        with pytest.raises(EmbeddingBudgetExceeded):
            await embedder.embed(["aaaaa"] * 50, EmbeddingTask.DOCUMENT)

        assert transport.call_count <= 2, (
            f"{transport.call_count} calls with concurrency 2 — the queue drained past the ceiling"
        )

    async def test_the_error_reports_both_sides_of_the_comparison(self) -> None:
        embedder = Embedder(
            FakeTransport(),
            settings(max_batch_size=1),
            count_tokens=lambda t: 1,
            ledger=TokenLedger(budget=3),
        )
        with pytest.raises(EmbeddingBudgetExceeded) as caught:
            await embedder.embed(["aaaaa"] * 5, EmbeddingTask.DOCUMENT)

        assert caught.value.budget == 3
        assert caught.value.spent > 3

    async def test_nothing_is_left_running_after_the_raise(self) -> None:
        """A cancelled sibling must not surface later as an orphaned task."""
        transport = FakeTransport()
        embedder = Embedder(
            transport,
            settings(max_batch_size=1, max_concurrency=2),
            count_tokens=lambda t: 1,
            ledger=TokenLedger(budget=1),
        )
        with pytest.raises(EmbeddingBudgetExceeded):
            await embedder.embed(["aaaaa"] * 40, EmbeddingTask.DOCUMENT)

        before = transport.call_count
        # Anything still scheduled would run on the next loop turn.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert transport.call_count == before, "a cancelled batch called the provider"


class TestTheProviderFigureDrivesTheCeiling:
    async def test_an_under_counting_estimator_cannot_hide_real_spend(self) -> None:
        """`estimate_tokens` under-counts masked text by about a quarter (§22).

        If the estimator drove the ledger, a job could pass a ceiling it had actually
        exceeded. The estimator chooses batch boundaries; the provider's figure is what
        is charged.
        """
        transport = FakeTransport()
        ledger = TokenLedger(budget=9)
        embedder = Embedder(
            transport,
            settings(max_batch_size=1, max_concurrency=1),
            # Claims one token per text. The transport bills five.
            count_tokens=lambda t: 1,
            ledger=ledger,
        )
        with pytest.raises(EmbeddingBudgetExceeded):
            await embedder.embed(["aaaaa"] * 10, EmbeddingTask.DOCUMENT)

        assert ledger.spent >= 10, "the ledger recorded the estimate, not the billed count"

    async def test_spend_matches_the_provider_response_exactly(self) -> None:
        transport = FakeTransport()
        embedder = Embedder(transport, settings(max_batch_size=10), count_tokens=lambda t: 1)
        await embedder.embed(["ab", "cde", "f"], EmbeddingTask.DOCUMENT)
        assert embedder.ledger.spent == 2 + 3 + 1


class TestNoCeilingRemainsAValidChoice:
    async def test_an_unset_budget_embeds_the_whole_batch(self) -> None:
        transport = FakeTransport()
        embedder = Embedder(transport, settings(), ledger=TokenLedger(budget=None))
        assert len(await embedder.embed(["a"] * 12, EmbeddingTask.DOCUMENT)) == 12

    def test_an_unset_budget_is_never_exhausted(self) -> None:
        assert TokenLedger(budget=None, spent=10**12).exhausted is False
