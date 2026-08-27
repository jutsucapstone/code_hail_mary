"""Fakes and fixtures for the embedding suite.

**Deliberately not `conftest.py`.** `packages/db/tests/conftest.py` already exists, and a
second `conftest` module under the `packages` tree makes `mypy packages` ambiguous — at
which point it checks nothing at all. The graph and connector suites hit this first; the
Makefile records it.

The fake transport is the seam that makes S6 testable without a credential, a network or
a cent. Almost every guarantee this slice makes — order preservation, backoff, error
classification, budget enforcement, truncation rejection, `task_type` correctness — is a
property of the *caller*, so a scripted transport proves them exactly.

`RECORDED_RESPONSE` is a **real provider response**, captured from
`gemini-embedding-001` in `asia-south1` through Application Default Credentials on
2026-08-27. Its two vectors have L2 norms of 0.583809 and 0.584159 — genuinely
unnormalised, which is what makes it useful: a synthetic fixture would have been written
already normalised and the normalisation tests would have passed against nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jutsu_retrieval.config import EmbeddingSettings
from jutsu_retrieval.errors import TransientEmbeddingError

__all__ = [
    "RECORDED_RESPONSE",
    "FakeTransport",
    "recorded_vector",
    "response_for",
    "settings",
    "transient",
]

FIXTURES = Path(__file__).parent / "fixtures"
RECORDED_RESPONSE: dict[str, Any] = json.loads(
    (FIXTURES / "gemini_embedding_001_768.json").read_text(encoding="utf-8")
)


def recorded_vector(index: int = 0) -> list[float]:
    """One real 768-dimensional vector, exactly as the provider returned it."""
    values = RECORDED_RESPONSE["predictions"][index]["embeddings"]["values"]
    return [float(value) for value in values]


def response_for(
    count: int, *, token_count: int = 10, truncated: bool = False, dimensions: int = 768
) -> dict[str, Any]:
    """A response shaped exactly like the recorded one, for `count` instances.

    The vector values are the recorded ones, so normalisation assertions run against real
    magnitudes. Only the *number* of predictions varies, which is what batching tests
    need.
    """
    base = recorded_vector(0)[:dimensions]
    return {
        "predictions": [
            {
                "embeddings": {
                    "statistics": {"token_count": token_count, "truncated": truncated},
                    "values": list(base),
                }
            }
            for _ in range(count)
        ]
    }


def settings(**overrides: Any) -> EmbeddingSettings:
    """Settings for a test. Project and location are placeholders — nothing dials out."""
    base: dict[str, Any] = {
        "project": "test-project",
        "location": "asia-south1",
        "max_batch_size": 250,
        "max_batch_tokens": 20_000,
        "max_concurrency": 1,
        "base_backoff_s": 0.0,
        "max_backoff_s": 0.0,
    }
    base.update(overrides)
    return EmbeddingSettings(**base)


@dataclass
class FakeTransport:
    """A scripted `EmbeddingTransport`. Records every call; never touches a network.

    `script` is consumed in order. An entry that is an exception is raised; anything else
    is returned. When the script runs out, a response matching the request size is
    synthesised, which is what most tests want.
    """

    script: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def predict(
        self, *, instances: list[dict[str, Any]], parameters: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append({"instances": list(instances), "parameters": dict(parameters)})

        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return dict(item)

        # Token count derived from the content, so a caller can check that result i
        # corresponds to input i. With a constant count every result is identical and an
        # order-preservation test cannot fail however badly the batching reorders.
        dimensions = int(parameters.get("outputDimensionality", 768))
        base = recorded_vector(0)[:dimensions]
        return {
            "predictions": [
                {
                    "embeddings": {
                        "statistics": {
                            "token_count": len(instance["content"]),
                            "truncated": False,
                        },
                        "values": list(base),
                    }
                }
                for instance in instances
            ]
        }

    # ------------------------------------------------------------------ helpers

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def task_types(self) -> set[str]:
        return {instance["task_type"] for call in self.calls for instance in call["instances"]}

    def contents(self) -> list[str]:
        return [instance["content"] for call in self.calls for instance in call["instances"]]

    def batch_sizes(self) -> list[int]:
        return [len(call["instances"]) for call in self.calls]


def transient(status: int = 429) -> TransientEmbeddingError:
    return TransientEmbeddingError("scripted transient failure", status=status)
