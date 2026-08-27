"""Batching, retry, normalisation and token accounting (spec §9.3).

No network and no credential. Every guarantee tested here is a property of the caller
rather than of the provider, which is why a scripted transport proves them exactly — and
why the two that are *not* callers' properties (that 768 output is unnormalised, that
over-long input truncates silently) were measured live and are baked into the recorded
fixture instead.
"""

from __future__ import annotations

import math

import pytest
from jutsu_retrieval.embeddings import (
    Embedder,
    Embedding,
    EmbeddingTask,
    TokenLedger,
    embed_batch,
    l2_normalise,
    plan_batches,
)
from jutsu_retrieval.errors import (
    EmbeddingBudgetExceeded,
    PermanentEmbeddingError,
    TransientEmbeddingError,
    TruncatedInput,
)
from retrieval_support import (
    FakeTransport,
    recorded_vector,
    response_for,
    settings,
    transient,
)

TEXTS = ["first chunk of text", "second chunk of text", "third chunk of text"]


def norm(vector: tuple[float, ...] | list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


async def embed(transport: FakeTransport, texts: list[str], **overrides: object) -> list[Embedding]:
    embedder = Embedder(transport, settings(**overrides), count_tokens=len)
    return await embedder.embed(texts, EmbeddingTask.DOCUMENT)


# --------------------------------------------------------------------------- normalise


class TestNormalisation:
    def test_the_recorded_provider_vector_is_not_normalised(self) -> None:
        """The measurement that made this code necessary.

        Captured live from `gemini-embedding-001` at `outputDimensionality=768`: L2 norm
        0.583809, not 1. A synthetic fixture would have been written already normalised
        and every assertion below would have passed against nothing.
        """
        assert norm(recorded_vector(0)) == pytest.approx(0.583809, abs=1e-5)
        assert norm(recorded_vector(1)) == pytest.approx(0.584159, abs=1e-5)

    def test_normalising_yields_unit_length(self) -> None:
        assert norm(l2_normalise(recorded_vector(0))) == pytest.approx(1.0, abs=1e-9)

    def test_normalising_preserves_direction(self) -> None:
        """Cosine is scale-invariant, so normalisation must not change any ranking."""
        original = recorded_vector(0)
        scaled = l2_normalise(original)
        cosine = sum(a * b for a, b in zip(original, scaled, strict=True)) / (
            norm(original) * norm(scaled)
        )
        assert cosine == pytest.approx(1.0, abs=1e-9)

    def test_a_zero_vector_does_not_become_nan(self) -> None:
        """A vector of zeros is a visible anomaly; a vector of NaN poisons every distance."""
        assert l2_normalise([0.0, 0.0, 0.0]) == (0.0, 0.0, 0.0)

    async def test_embedded_vectors_come_back_normalised(self) -> None:
        results = await embed(FakeTransport(), TEXTS)
        for embedding in results:
            assert norm(embedding.vector) == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------- batching


class TestBatching:
    def test_batches_respect_the_size_bound(self) -> None:
        counts = [1] * 600
        assert plan_batches(counts, settings(max_batch_size=250)) == [
            (0, 250),
            (250, 500),
            (500, 600),
        ]

    def test_batches_respect_the_token_bound(self) -> None:
        """The bound that actually matters: 250 chunks of 768 tokens is not one request."""
        counts = [800] * 10
        batches = plan_batches(counts, settings(max_batch_size=250, max_batch_tokens=2000))
        assert all(sum(counts[a:b]) <= 2000 for a, b in batches)
        assert batches[0] == (0, 2)

    def test_an_oversized_item_still_gets_a_batch(self) -> None:
        """Refusing it is the embedder's job, and it refuses with an index."""
        batches = plan_batches([50, 5000, 50], settings(max_batch_tokens=100))
        assert (1, 2) in batches

    def test_every_item_appears_exactly_once(self) -> None:
        counts = [7] * 137
        batches = plan_batches(counts, settings(max_batch_size=10, max_batch_tokens=40))
        covered = [index for a, b in batches for index in range(a, b)]
        assert covered == list(range(137))

    def test_an_empty_input_produces_no_batches(self) -> None:
        assert plan_batches([], settings()) == []

    async def test_the_batch_size_is_configurable(self) -> None:
        """250 is a measured default, not a provider guarantee."""
        transport = FakeTransport()
        await embed(transport, [f"text {i}" for i in range(10)], max_batch_size=3)
        assert transport.batch_sizes() == [3, 3, 3, 1]

    async def test_order_is_preserved_across_batches(self) -> None:
        """The caller pairs results with chunk ids positionally.

        A reordering here attaches every vector to the wrong chunk, silently, with no
        error anywhere downstream.
        """
        texts = [f"text {index}" + "x" * index for index in range(25)]
        embedder = Embedder(
            FakeTransport(),
            settings(max_batch_size=4, max_concurrency=4),
            count_tokens=lambda t: 1,
        )
        results = await embedder.embed(texts, EmbeddingTask.DOCUMENT)

        # The fake reports each instance's content length as its token count, so this
        # pins result i to input i. Concurrency across batches means the requests may
        # complete out of order; the results may not come back out of order.
        assert [embedding.token_count for embedding in results] == [len(text) for text in texts]

    async def test_no_input_means_no_request(self) -> None:
        transport = FakeTransport()
        assert await embed(transport, []) == []
        assert transport.call_count == 0


# --------------------------------------------------------------------------- task_type


class TestTaskType:
    async def test_document_and_query_send_different_values(self) -> None:
        """§9.3's named hazard, asserted on the outgoing request.

        Measured live on identical text, cosine(DOCUMENT, QUERY) = 0.915317 — the two are
        genuinely different vectors, so sending one value for both silently costs recall.
        """
        document, query = FakeTransport(), FakeTransport()
        await Embedder(document, settings()).embed(TEXTS, EmbeddingTask.DOCUMENT)
        await Embedder(query, settings()).embed(TEXTS, EmbeddingTask.QUERY)

        assert document.task_types() == {"RETRIEVAL_DOCUMENT"}
        assert query.task_types() == {"RETRIEVAL_QUERY"}
        assert document.task_types() != query.task_types()

    async def test_the_output_dimensionality_is_sent(self) -> None:
        transport = FakeTransport()
        await embed(transport, TEXTS)
        assert transport.calls[0]["parameters"]["outputDimensionality"] == 768


# --------------------------------------------------------------------------- retry


class TestRetryAndErrorClassification:
    async def test_a_transient_failure_is_retried_then_succeeds(self) -> None:
        transport = FakeTransport(script=[transient(429), transient(503)])
        results = await embed(transport, TEXTS)
        assert len(results) == len(TEXTS)
        assert transport.call_count == 3

    async def test_retries_are_bounded(self) -> None:
        transport = FakeTransport(script=[transient(429) for _ in range(10)])
        with pytest.raises(TransientEmbeddingError):
            await embed(transport, TEXTS, max_attempts=4)
        assert transport.call_count == 4

    async def test_a_permanent_failure_is_never_retried(self) -> None:
        """The bug this guards against: retrying a 400 spends real quota to be told the
        same thing, and on a corpus-sized job it spends a great deal of it."""
        transport = FakeTransport(script=[PermanentEmbeddingError("bad request", status=400)])
        with pytest.raises(PermanentEmbeddingError):
            await embed(transport, TEXTS)
        assert transport.call_count == 1

    async def test_backoff_grows_and_is_jittered(self) -> None:
        delays: list[float] = []

        async def record(seconds: float) -> None:
            delays.append(seconds)

        # Four attempts need four failures to exhaust them; three would succeed on
        # the fourth and prove nothing about the backoff.
        transport = FakeTransport(script=[transient(429) for _ in range(4)])
        embedder = Embedder(
            transport,
            settings(base_backoff_s=1.0, max_backoff_s=60.0, max_attempts=4),
            sleep=record,
        )
        with pytest.raises(TransientEmbeddingError):
            await embedder.embed(TEXTS, EmbeddingTask.DOCUMENT)

        assert len(delays) == 3
        # Full jitter: each delay is drawn from [0, ceiling] where the ceiling doubles.
        # Unjittered backoff retries every concurrent batch at the same instant and trips
        # the per-minute quota again.
        assert all(delay >= 0 for delay in delays)
        assert max(delays) <= 60.0

    async def test_a_timeout_is_treated_as_transient(self) -> None:
        transport = FakeTransport(script=[TransientEmbeddingError("embedding request timed out")])
        results = await embed(transport, TEXTS)
        assert len(results) == len(TEXTS)
        assert transport.call_count == 2


# --------------------------------------------------------------------------- truncation


class TestTruncation:
    async def test_a_truncated_response_is_refused(self) -> None:
        """Measured: 2081 tokens returned `truncated=True` under HTTP 200, with a
        well-formed vector describing only a prefix. Nothing in the status says so."""
        transport = FakeTransport(script=[response_for(3, truncated=True, token_count=2081)])
        with pytest.raises(TruncatedInput) as caught:
            await embed(transport, TEXTS)
        assert caught.value.token_count == 2081

    async def test_the_refusal_names_the_global_index(self) -> None:
        """So a caller can re-chunk the offending document rather than the whole corpus."""
        transport = FakeTransport(script=[response_for(2), response_for(2, truncated=True)])
        with pytest.raises(TruncatedInput) as caught:
            await embed(transport, ["a", "b", "c", "d"], max_batch_size=2)
        assert caught.value.index == 2

    async def test_a_wrong_dimension_response_is_refused(self) -> None:
        transport = FakeTransport(script=[response_for(3, dimensions=512)])
        with pytest.raises(PermanentEmbeddingError, match="768 dimensions"):
            await embed(transport, TEXTS)

    async def test_a_short_response_is_refused(self) -> None:
        """A partial response would misalign every vector after the gap."""
        transport = FakeTransport(script=[response_for(2)])
        with pytest.raises(PermanentEmbeddingError, match="2 embeddings for 3 inputs"):
            await embed(transport, TEXTS)


# --------------------------------------------------------------------------- accounting


class TestTokenAccounting:
    async def test_the_providers_count_is_recorded(self) -> None:
        """Not an estimate. It is what was billed, and therefore what goes in the column."""
        results = await embed(FakeTransport(), TEXTS)
        assert [embedding.token_count for embedding in results] == [len(t) for t in TEXTS]

    async def test_the_ledger_totals_tokens_and_requests(self) -> None:
        embedder = Embedder(FakeTransport(), settings(max_batch_size=2), count_tokens=lambda t: 1)
        await embedder.embed(["aaa", "bb", "c"], EmbeddingTask.DOCUMENT)
        assert embedder.ledger.spent == 6  # 3 + 2 + 1 characters
        assert embedder.ledger.requests == 2

    async def test_the_budget_stops_the_job(self) -> None:
        """§20's cost guardrail, in code rather than in the billing console."""
        embedder = Embedder(
            FakeTransport(),
            settings(max_batch_size=1),
            count_tokens=lambda t: 1,
            ledger=TokenLedger(budget=5),
        )
        with pytest.raises(EmbeddingBudgetExceeded) as caught:
            await embedder.embed(["aaa", "bbb", "ccc"], EmbeddingTask.DOCUMENT)
        assert caught.value.budget == 5
        assert caught.value.spent > 5

    async def test_no_budget_means_no_ceiling(self) -> None:
        embedder = Embedder(FakeTransport(), settings(), ledger=TokenLedger(budget=None))
        assert len(await embedder.embed(TEXTS, EmbeddingTask.DOCUMENT)) == 3


class TestSpecSignature:
    async def test_embed_batch_returns_plain_vectors(self) -> None:
        """§9.3's shape, kept reachable — though the pipeline uses `Embedder` so the
        provider's token count is not discarded."""
        vectors = await embed_batch(
            TEXTS, EmbeddingTask.DOCUMENT, transport=FakeTransport(), settings=settings()
        )
        assert len(vectors) == 3
        assert all(len(vector) == 768 for vector in vectors)
        assert all(norm(vector) == pytest.approx(1.0, abs=1e-9) for vector in vectors)
