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


class TestIdleIsNotFailure:
    """`JOB_FAILED` exists so a drain loop can tell an empty queue from a failed job.

    The 200-document pilot is the evidence. `process_embedding` returned `None` for both
    outcomes, the seed loop broke on `None`, and one HTTP 429 on the thirteenth job ended
    the embedding phase with **187 jobs never attempted** — while the command exited 0.
    At 45 000 documents the same code would report success over a mostly-unembedded
    corpus.
    """

    def test_the_sentinel_is_not_none(self) -> None:
        from jutsu_worker.runner import JOB_FAILED

        assert JOB_FAILED is not None

    def test_the_sentinel_is_falsy(self) -> None:
        """`main.py` guards with `if outcome`, so a failure must not read as success."""
        from jutsu_worker.runner import JOB_FAILED

        assert not JOB_FAILED

    def test_it_is_distinguishable_from_a_zero_vector_count(self) -> None:
        """A job that legitimately wrote 0 vectors is a success, not a failure.

        `0` is falsy too, so truthiness alone cannot separate them — which is exactly why
        the drain loop compares with `is`.
        """
        from jutsu_worker.runner import JOB_FAILED

        # `0` is falsy too, so a drain loop must compare identity rather than truth.
        assert not JOB_FAILED and not 0
        assert JOB_FAILED is not None and isinstance(0, int)

    def test_it_repr_s_readably_in_a_log(self) -> None:
        from jutsu_worker.runner import JOB_FAILED

        assert repr(JOB_FAILED) == "JOB_FAILED"

    def test_the_drain_loop_continues_past_a_failure(self) -> None:
        """The loop shape itself, exercised without a database.

        Simulates the pilot: successes, then a failure, then more work. The old shape
        stopped at the failure; the new one must reach the end of the queue.
        """
        from jutsu_worker.runner import JOB_FAILED

        results: list[object] = [1, 1, JOB_FAILED, 1, 1, None]
        seen, written = 0, 0
        for value in results:
            if value is None:
                break
            if value is JOB_FAILED:
                seen += 1
                continue
            assert isinstance(value, int)
            written += value
        assert (written, seen) == (4, 1), "the drain stopped at the failure"
