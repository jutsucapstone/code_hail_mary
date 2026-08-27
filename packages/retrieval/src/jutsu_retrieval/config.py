"""Embedding configuration (spec §9.3, §20).

Every number here was **measured against the live API in `asia-south1` on 2026-08-27**,
not taken from documentation, and the measurement is recorded beside it. Where the
measurement disagrees with the spec, the disagreement is stated rather than quietly
applied.

Nothing here is a provider guarantee. The batch size in particular is a *measured
configurable default* — 250 instances succeeded in one request, but that is an
observation about one model in one region on one day, so it is a setting with a token
budget and a rate limiter around it rather than a constant to rely on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from jutsu_db.models import EMBEDDING_DIM

__all__ = [
    "DEFAULT_MAX_BATCH_SIZE",
    "MEASURED_INPUT_TOKEN_LIMIT",
    "EmbeddingSettings",
    "MissingEmbeddingSettings",
    "get_embedding_settings",
]

#: `gemini-embedding-001`, verified callable in asia-south1. Chosen over
#: `text-embedding-004` on multilingual support — the corpus is not ASCII-only — and over
#: `text-embedding-005`, which is English-only and therefore disqualified regardless of
#: how well it would otherwise fit.
DEFAULT_MODEL: Final = "gemini-embedding-001"

#: Measured: 250 instances returned in one request in 16.8 s. §9.3 says 100. The
#: deviation is deliberate and the reason is that the binding constraint turned out to be
#: **requests per minute**, not instances per request — a 429 on
#: `online_prediction_requests_per_base_model` arrived after roughly eight rapid
#: requests, long before any batch-size ceiling. Fewer, larger requests is therefore
#: strictly better. Configurable, because 250 is an observation and not a promise.
DEFAULT_MAX_BATCH_SIZE: Final = 250

#: A second bound on the same request, in tokens. Batch size alone is not enough: 250
#: chunks of 768 tokens is far more than any single request should carry, and the failure
#: mode of finding that out from the provider is a rejected batch mid-run.
DEFAULT_MAX_BATCH_TOKENS: Final = 20_000

#: Measured truncation threshold. 1761 reported tokens came back `truncated=False`; 2081
#: came back `truncated=True` **with HTTP 200**. The limit is 2048.
#:
#: Silent truncation is the reason `embeddings.py` treats the flag as a hard failure: an
#: over-long chunk yields a vector that does not represent the chunk, and nothing in the
#: response status says so.
MEASURED_INPUT_TOKEN_LIMIT: Final = 2048

#: 250 instances took 16.8 s. A timeout below that turns the largest working batch into a
#: guaranteed failure, so this has headroom over the slowest measured request.
DEFAULT_REQUEST_TIMEOUT_S: Final = 120.0

#: Low on purpose. Requests per minute is the constraint, so concurrency buys throughput
#: only until it trips the quota — at which point every request in flight fails together
#: and the backoff has to unwind them all.
DEFAULT_MAX_CONCURRENCY: Final = 2


class MissingEmbeddingSettings(RuntimeError):
    """Required configuration is absent. Raised at first use, never defaulted."""


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    """Where the model is, and every bound the client operates under.

    `dimensions` is validated against the database's `EMBEDDING_DIM` at construction.
    They cannot be allowed to disagree: the column is `vector(768)` with an HNSW index
    already built on it, so a mismatch is not a configuration error that surfaces as a
    warning — it is an insert that fails, or worse, a silent switch to a different vector
    space that only shows up as degraded recall.
    """

    project: str
    location: str
    model: str = DEFAULT_MODEL
    dimensions: int = EMBEDDING_DIM
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE
    max_batch_tokens: int = DEFAULT_MAX_BATCH_TOKENS
    max_input_tokens: int = MEASURED_INPUT_TOKEN_LIMIT
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    #: Attempts *including* the first. 5 gives four retries, which at the backoff below
    #: spans about a minute — long enough to ride out a per-minute quota window.
    max_attempts: int = 5
    base_backoff_s: float = 1.0
    max_backoff_s: float = 60.0
    #: Hard ceiling on input tokens for one job. §20 asks for cost guardrails on day one;
    #: this is the one that lives in the code rather than in the billing console, and it
    #: is what stops a retry loop re-embedding a corpus. `None` disables it, which is
    #: only appropriate for a test.
    token_budget: int | None = None

    def __post_init__(self) -> None:
        if self.dimensions != EMBEDDING_DIM:
            raise MissingEmbeddingSettings(
                f"dimensions={self.dimensions} but the chunks.embedding column is "
                f"vector({EMBEDDING_DIM}) with an HNSW index already built on it. "
                f"Changing the width is a migration and a full re-index, not a setting."
            )
        if self.max_batch_size < 1:
            raise MissingEmbeddingSettings("max_batch_size must be at least 1")
        if self.max_batch_tokens < 1:
            raise MissingEmbeddingSettings("max_batch_tokens must be at least 1")
        if self.max_attempts < 1:
            raise MissingEmbeddingSettings("max_attempts must be at least 1")

    @property
    def endpoint(self) -> str:
        """The regional `:predict` URL. Regional, per §20 — Vertex AI is not global here,
        and the region is a data-residency decision rather than a latency one."""
        return (
            f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{self.project}"
            f"/locations/{self.location}/publishers/google/models/{self.model}:predict"
        )

    def __repr__(self) -> str:
        """No credential is held here, but the shape matches `GraphSettings` so that
        nobody has to check whether this one is safe to log."""
        return (
            f"EmbeddingSettings(project={self.project!r}, location={self.location!r}, "
            f"model={self.model!r}, dimensions={self.dimensions})"
        )


def get_embedding_settings() -> EmbeddingSettings:
    """Read configuration from the environment (§4.10).

    No default for the project. A default would silently bill whichever project happened
    to be configured, and the one thing worse than a missing project id is the wrong one.
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    location = os.environ.get("VERTEX_LOCATION", "").strip()

    if not project or not location:
        raise MissingEmbeddingSettings(
            "GOOGLE_CLOUD_PROJECT and VERTEX_LOCATION must both be set. Copy "
            ".env.example to .env for local development; in staging and production they "
            "come from the deployment environment."
        )

    return EmbeddingSettings(
        project=project,
        location=location,
        model=os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL),
        dimensions=int(os.environ.get("EMBEDDING_DIM", EMBEDDING_DIM)),
        max_batch_size=int(os.environ.get("EMBEDDING_MAX_BATCH", DEFAULT_MAX_BATCH_SIZE)),
        max_batch_tokens=int(
            os.environ.get("EMBEDDING_MAX_BATCH_TOKENS", DEFAULT_MAX_BATCH_TOKENS)
        ),
        max_concurrency=int(os.environ.get("EMBEDDING_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)),
    )
