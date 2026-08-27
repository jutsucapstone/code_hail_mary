"""One bounded live call against Vertex AI. Skipped unless explicitly enabled.

**This is the test that mocks cannot replace.** Everything else in this package proves a
property of the caller against a scripted transport, which is the right way to test
batching, retry and budget enforcement. None of it proves that the credential works, that
the model answers, that 768 comes back, or that the statistics field is still called what
it was called — and a slice that claimed to be production-ready on mocks alone would be
claiming exactly the thing it had not checked.

**It costs money.** Two short strings, roughly twenty input tokens, which is a small
fraction of one paisa against the ₹2,500 monthly budget on billing account
`01770F-36BD13-9F6070`. It is gated behind `JUTSU_LIVE_EMBEDDING_SMOKE=1` so CI never
runs it and nobody spends anything by accident.

    JUTSU_LIVE_EMBEDDING_SMOKE=1 uv run --env-file .env pytest \\
        packages/retrieval/tests/test_live_smoke.py -q

It embeds **two sentences**. It does not touch the corpus, and it writes nothing to
Postgres.
"""

from __future__ import annotations

import math
import os

import pytest
from jutsu_retrieval.client import VertexTransport
from jutsu_retrieval.config import get_embedding_settings
from jutsu_retrieval.embeddings import Embedder, EmbeddingTask

LIVE_FLAG = "JUTSU_LIVE_EMBEDDING_SMOKE"

#: Two short, innocuous strings. Nothing from the corpus, nothing sensitive, and few
#: enough tokens that the spend is not worth measuring.
SAMPLES = [
    "The migration ran cleanly against staging on Tuesday morning.",
    "Retrieval latency sat at roughly two hundred milliseconds.",
]

pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_FLAG) != "1",
    reason=f"live provider call; set {LIVE_FLAG}=1 to run (spends a small amount)",
)


def norm(vector: tuple[float, ...]) -> float:
    return math.sqrt(sum(value * value for value in vector))


class TestLiveProviderPath:
    async def test_the_real_provider_path_works_end_to_end(self) -> None:
        """Application Default Credentials, the real model, the real region.

        Asserts the four things the recorded fixture cannot keep honest on its own: that
        the credential resolves, that the model is reachable, that the width is what the
        column expects, and that the statistics the token accounting depends on are still
        present under the names it reads.
        """
        settings = get_embedding_settings()
        transport = VertexTransport(settings)
        embedder = Embedder(transport, settings)

        try:
            results = await embedder.embed(SAMPLES, EmbeddingTask.DOCUMENT)
        finally:
            await transport.aclose()

        assert len(results) == len(SAMPLES)

        for embedding in results:
            # The width the chunks.embedding column and its HNSW index were built for.
            assert len(embedding.vector) == settings.dimensions == 768
            # Normalised by this package. The provider returns roughly 0.5838 at 768
            # dimensions, so a unit norm here proves our own step ran.
            assert norm(embedding.vector) == pytest.approx(1.0, abs=1e-9)
            # The provider's own count, which is what gets written to the column.
            assert embedding.token_count > 0

        # Nothing was truncated: `Embedder` raises `TruncatedInput` rather than returning,
        # so reaching this line at all is the assertion.
        assert embedder.ledger.spent == sum(e.token_count for e in results)
        assert embedder.ledger.requests >= 1

    async def test_the_two_task_types_produce_different_vectors(self) -> None:
        """§9.3's hazard, confirmed against the live model rather than against a fixture.

        Measured at 0.915317 cosine on identical text during S6 readiness. The assertion
        is deliberately loose — the point is that the two differ, not that they differ by
        a particular amount, and pinning the number would make this a change detector for
        the provider's weights.
        """
        settings = get_embedding_settings()
        transport = VertexTransport(settings)
        embedder = Embedder(transport, settings)

        try:
            document = await embedder.embed(SAMPLES[:1], EmbeddingTask.DOCUMENT)
            query = await embedder.embed(SAMPLES[:1], EmbeddingTask.QUERY)
        finally:
            await transport.aclose()

        similarity = sum(a * b for a, b in zip(document[0].vector, query[0].vector, strict=True))
        assert similarity < 0.999, "task_type made no difference — check it reaches the wire"
        assert similarity > 0.5, "the two are unrelated — check the request shape"
