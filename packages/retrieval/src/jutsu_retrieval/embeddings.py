"""Embedding: batching, retry, normalisation and token accounting (spec §9.3).

Everything measured against the live API in `asia-south1` on 2026-08-27 is applied here,
including the two findings that changed the design:

  * **MRL-truncated output is not normalised.** At the default 3072 dimensions the vector
    came back with an L2 norm of exactly 1.000000; at `outputDimensionality=768` the norm
    was **0.583809**. Cosine distance is scale-invariant so `vector_cosine_ops` would
    still rank correctly, but the vectors are normalised here anyway — the ops class is a
    schema decision that may change, and a store of mixed-magnitude vectors is a trap for
    whoever changes it.
  * **Over-long input is truncated silently, under HTTP 200.** 2081 tokens came back
    `truncated=True` with a well-formed vector describing only a prefix. That is treated
    as a hard failure and never returned, let alone persisted.

**`task_type` is not optional and not cosmetic.** §9.3 warns that using one value for
both documents and queries costs several points of recall and stays invisible until eval.
Measured on identical text, `cosine(RETRIEVAL_DOCUMENT, RETRIEVAL_QUERY)` is **0.915317** —
the two are genuinely different vectors, so the distinction is real and the tests assert
it on the outgoing request.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from jutsu_core import TokenCounter, estimate_tokens

from jutsu_retrieval.client import EmbeddingTransport
from jutsu_retrieval.config import EmbeddingSettings
from jutsu_retrieval.errors import (
    EmbeddingBudgetExceeded,
    PermanentEmbeddingError,
    TransientEmbeddingError,
    TruncatedInput,
)

__all__ = [
    "Embedder",
    "Embedding",
    "EmbeddingTask",
    "TokenLedger",
    "l2_normalise",
    "plan_batches",
]


class EmbeddingTask(StrEnum):
    """The two `task_type` values this pipeline uses.

    Separate members rather than a boolean, because §9.3's failure mode is passing the
    *wrong one*, and a boolean makes that a one-character mistake at the call site.
    """

    DOCUMENT = "RETRIEVAL_DOCUMENT"
    QUERY = "RETRIEVAL_QUERY"


@dataclass(frozen=True, slots=True)
class Embedding:
    """One vector, and what the provider says it cost.

    `token_count` is the provider's own figure, not an estimate. It is what was billed,
    which makes it both the authoritative value for `chunks.token_count` and the only
    honest input to cost accounting.
    """

    vector: tuple[float, ...]
    token_count: int


@dataclass(slots=True)
class TokenLedger:
    """Running token spend for one job, and the ceiling it may not cross.

    §20 wants cost guardrails on day one. The budget alert in the billing console is the
    backstop that tells a human afterwards; this is the one that stops the job.

    **`spent` only ever moves on a provider figure.** `charge` is called with the sum of
    `Embedding.token_count`, which is what the response said was billed — never with
    `estimate_tokens`, which under-counts masked text by about a quarter. An estimate
    driving the ceiling would let a job overrun it and still report itself inside.

    Two guards, not one. `charge` closes the door after a batch settles; `check` refuses
    to open it for the next one. Without the second, a ceiling reached by batch three is
    still paid for by every batch already queued behind it.
    """

    budget: int | None = None
    spent: int = 0
    requests: int = 0

    @property
    def exhausted(self) -> bool:
        return self.budget is not None and self.spent >= self.budget

    def _exceeded(self) -> EmbeddingBudgetExceeded:
        assert self.budget is not None  # noqa: S101 - only reachable with a ceiling set
        return EmbeddingBudgetExceeded(
            f"token budget exhausted after {self.spent} tokens",
            spent=self.spent,
            budget=self.budget,
        )

    def check(self) -> None:
        """Refuse before spending. Called immediately before every provider request.

        This is the half of the guarantee that costs money when it is missing: charging
        after the fact detects an overrun, it does not prevent the next one.
        """
        if self.exhausted:
            raise self._exceeded()

    def charge(self, tokens: int) -> None:
        self.spent += tokens
        if self.budget is not None and self.spent > self.budget:
            raise self._exceeded()


def l2_normalise(vector: Sequence[float]) -> tuple[float, ...]:
    """Scale a vector to unit length.

    A zero vector is returned unchanged rather than producing NaN. The provider has never
    been observed to return one, and if it ever does, a vector of zeros is a visible
    anomaly while a vector of NaN silently poisons every distance computation it touches.
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return tuple(vector)
    return tuple(value / norm for value in vector)


def plan_batches(token_counts: Sequence[int], settings: EmbeddingSettings) -> list[tuple[int, int]]:
    """Half-open index ranges, each within both the size and the token bound.

    Two bounds, not one. Batch size alone permits 250 chunks of 768 tokens in a single
    request, which is far more than one request should carry; the token bound is what
    actually keeps a request sane, and the size bound is what respects the provider's
    per-request instance cap.

    An item larger than the whole token bound still gets its own batch rather than being
    dropped — refusing it is `Embedder`'s job, and it refuses with the index, which a
    caller can act on.
    """
    batches: list[tuple[int, int]] = []
    start = 0
    tokens = 0

    for index, count in enumerate(token_counts):
        would_be = index - start + 1
        if start < index and (
            would_be > settings.max_batch_size or tokens + count > settings.max_batch_tokens
        ):
            batches.append((start, index))
            start, tokens = index, 0
        tokens += count

    if start < len(token_counts):
        batches.append((start, len(token_counts)))
    return batches


class Embedder:
    """Turns text into vectors, in order, within budget.

    Order preservation is a contract, not an implementation detail: the caller pairs the
    results with chunk ids positionally, so a reordering silently attaches every vector to
    the wrong chunk. There is a test for it because the failure is invisible.
    """

    __slots__ = ("_count_tokens", "_ledger", "_semaphore", "_settings", "_sleep", "_transport")

    def __init__(
        self,
        transport: EmbeddingTransport,
        settings: EmbeddingSettings,
        *,
        count_tokens: TokenCounter = estimate_tokens,
        ledger: TokenLedger | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._settings = settings
        self._count_tokens = count_tokens
        self._ledger = ledger or TokenLedger(budget=settings.token_budget)
        # Injected so a retry test does not spend a real minute proving the backoff grows.
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)

    @property
    def ledger(self) -> TokenLedger:
        return self._ledger

    async def embed(self, texts: Sequence[str], task: EmbeddingTask) -> list[Embedding]:
        """Embed every text, in order.

        Batches are dispatched concurrently up to `max_concurrency`, which is low by
        design: the measured constraint is requests per minute, so more concurrency buys
        throughput only until it trips the quota, at which point everything in flight
        fails together.
        """
        if not texts:
            return []

        counts = [self._count_tokens(text) for text in texts]
        batches = plan_batches(counts, self._settings)

        async def run(bounds: tuple[int, int]) -> list[Embedding]:
            async with self._semaphore:
                # Inside the semaphore, immediately before the call. Checked here rather
                # than before dispatch because the ledger moves while this batch waits:
                # a budget exhausted by an earlier batch must stop this one, and the only
                # moment that is knowable is the moment before spending.
                self._ledger.check()
                start, end = bounds
                return await self._embed_one_batch(list(texts[start:end]), task, offset=start)

        # Explicit tasks rather than a bare `gather`, so the rest can be cancelled. A
        # `gather` that propagates the first exception leaves its siblings running: they
        # acquire the semaphore and call the provider after the budget is already gone,
        # which is the exact failure the ceiling exists to prevent — and it is silent,
        # because the caller already has its exception by then.
        tasks = [asyncio.create_task(run(bounds)) for bounds in batches]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for pending in tasks:
                pending.cancel()
            # Awaited so nothing is left orphaned mid-request; exceptions from the
            # cancelled siblings are discarded in favour of the one being raised.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return [embedding for batch in results for embedding in batch]

    async def _embed_one_batch(
        self, texts: list[str], task: EmbeddingTask, *, offset: int
    ) -> list[Embedding]:
        instances = [{"content": text, "task_type": task.value} for text in texts]
        parameters = {"outputDimensionality": self._settings.dimensions}

        payload = await self._with_retries(instances, parameters)
        predictions = payload.get("predictions", [])

        if len(predictions) != len(texts):
            # A partial response would silently misalign every vector after the gap.
            raise PermanentEmbeddingError(
                f"provider returned {len(predictions)} embeddings for {len(texts)} inputs"
            )

        embeddings: list[Embedding] = []
        for index, prediction in enumerate(predictions):
            block = prediction.get("embeddings", {})
            statistics = block.get("statistics", {})
            token_count = int(statistics.get("token_count", 0) or 0)

            if bool(statistics.get("truncated", False)):
                # HTTP 200, well-formed vector, wrong answer. Refused here so it can never
                # reach the database.
                raise TruncatedInput(
                    "provider truncated the input; the vector describes only a prefix",
                    index=offset + index,
                    token_count=token_count,
                )

            values = block.get("values")
            if not isinstance(values, list) or len(values) != self._settings.dimensions:
                raise PermanentEmbeddingError(
                    f"expected {self._settings.dimensions} dimensions, got "
                    f"{len(values) if isinstance(values, list) else 'none'}"
                )

            embeddings.append(Embedding(vector=l2_normalise(values), token_count=token_count))

        self._ledger.charge(sum(embedding.token_count for embedding in embeddings))
        return embeddings

    async def _with_retries(
        self, instances: list[dict[str, Any]], parameters: dict[str, Any]
    ) -> dict[str, Any]:
        """Exponential backoff with jitter, on transient failures only.

        Full jitter rather than a fixed schedule: when a per-minute quota rejects several
        concurrent batches at once, an unjittered backoff retries them all at the same
        instant and trips it again.

        `PermanentEmbeddingError` is re-raised immediately and deliberately. A 400 will be
        rejected identically every time, and retrying it on a corpus-sized job spends real
        quota to receive the same answer.
        """
        last: Exception | None = None

        for attempt in range(self._settings.max_attempts):
            self._ledger.requests += 1
            try:
                return await self._transport.predict(instances=instances, parameters=parameters)
            except TransientEmbeddingError as error:
                last = error
                if attempt == self._settings.max_attempts - 1:
                    break
                ceiling = min(
                    self._settings.base_backoff_s * (2**attempt), self._settings.max_backoff_s
                )
                # S311: not cryptographic, and must not be — this is jitter, and a
                # CSPRNG here would buy nothing while making the retry untestable.
                await self._sleep(random.uniform(0.0, ceiling))  # noqa: S311

        assert last is not None  # noqa: S101 - the loop cannot exit without one
        raise last


#: The §9.3 signature, kept as a module-level helper so the spec's shape is reachable.
async def embed_batch(
    texts: Sequence[str],
    task_type: EmbeddingTask,
    *,
    transport: EmbeddingTransport,
    settings: EmbeddingSettings,
    count_tokens: TokenCounter = estimate_tokens,
) -> list[list[float]]:
    """§9.3's `embed_batch`, returning plain vectors.

    Loses the token counts, which is why the pipeline uses `Embedder` directly — the
    provider's count is the authoritative value for `chunks.token_count` and discarding it
    would put an estimate back in the database.
    """
    embedder = Embedder(transport, settings, count_tokens=count_tokens)
    return [list(embedding.vector) for embedding in await embedder.embed(texts, task_type)]


#: Measured latency, for anyone sizing a job. One instance ~620-860 ms; 10 ~1.4 s;
#: 50 ~3.3 s; 250 ~16.8 s. Roughly 65 ms per instance at the largest measured batch, on
#: top of about half a second of fixed cost.
MEASURED_LATENCY_NOTE: Final = "1:~0.7s 10:1.4s 50:3.3s 250:16.8s (asia-south1, 2026-08-27)"
