"""What `classify` does with a budget exhaustion, and why it is not the neighbours.

`classify` decides between a queue that drains and one that spins, and its
`BUDGET_EXHAUSTED` branch had no test — which matters now that the ceiling it reports is
actually enforced. Two mistakes are available here and both are quiet:

* **Permanent.** The document is not at fault; the *run* hit its ceiling. Marked
  permanent, every document still queued when the budget ran out is dropped, and raising
  the ceiling does not bring them back.
* **Retryable but indistinguishable from a transient provider error.** Then an operator
  reading `failure_kind` cannot tell "Vertex was briefly unavailable" from "you need to
  decide whether to spend more money", which are different actions.

Kept out of `packages/retrieval` deliberately: this is the worker's contract, and a
retrieval test reaching into `apps/worker` would invert the dependency the packages are
arranged to keep one-way.
"""

from __future__ import annotations

import pytest
from jutsu_core.errors import ExtractionRejected
from jutsu_retrieval.errors import (
    EmbeddingBudgetExceeded,
    PermanentEmbeddingError,
    TransientEmbeddingError,
    TruncatedInput,
)
from jutsu_worker.ingest import classify
from jutsu_worker.jobs import FailureKind


def _budget() -> EmbeddingBudgetExceeded:
    return EmbeddingBudgetExceeded("token budget exhausted after 11 tokens", spent=11, budget=10)


class TestBudgetExhaustion:
    def test_it_has_its_own_failure_kind(self) -> None:
        kind, _ = classify(_budget())
        assert kind is FailureKind.BUDGET_EXHAUSTED

    def test_it_is_retryable(self) -> None:
        """The ceiling moving is an operator decision, not a new job."""
        _, retryable = classify(_budget())
        assert retryable is True

    def test_it_is_not_a_permanent_embedding_failure(self) -> None:
        kind, _ = classify(_budget())
        assert kind is not FailureKind.EMBEDDING_PERMANENT

    def test_it_is_distinguishable_from_a_transient_provider_error(self) -> None:
        """Different operator actions, so they must not share a kind."""
        budget_kind, _ = classify(_budget())
        transient_kind, _ = classify(TransientEmbeddingError("429"))
        assert budget_kind is not transient_kind


class TestTheNeighbouringKinds:
    """Pinned so a future branch cannot absorb the budget case by accident."""

    @pytest.mark.parametrize(
        ("error", "expected", "retryable"),
        [
            (
                TruncatedInput("prefix only", index=0, token_count=2081),
                FailureKind.EMBEDDING_PERMANENT,
                False,
            ),
            (PermanentEmbeddingError("400"), FailureKind.EMBEDDING_PERMANENT, False),
            (TransientEmbeddingError("429"), FailureKind.EMBEDDING_TRANSIENT, True),
        ],
        ids=["truncated", "permanent", "transient"],
    )
    def test_embedding_errors_keep_their_kinds(
        self, error: Exception, expected: FailureKind, retryable: bool
    ) -> None:
        assert classify(error) == (expected, retryable)

    def test_an_unrecognised_error_is_internal_and_retryable(self) -> None:
        """The safe direction: bounded attempts, then dead-letter."""
        assert classify(ExtractionRejected("unknown")) == (FailureKind.INTERNAL, True)
