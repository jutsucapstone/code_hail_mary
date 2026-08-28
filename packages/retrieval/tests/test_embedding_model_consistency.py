"""One model name, in every place that states one.

The audit found `.env` on `text-embedding-004` while the code default, `.env.example` and
ADR 0009 all said `gemini-embedding-001`. That drift is expensive in a specific way: every
measured property this package relies on — the 0.58 L2 norm at `outputDimensionality=768`,
the 2048-token truncation boundary, the 250-instance batch, the recorded response fixture —
was established against `gemini-embedding-001` in `asia-south1` on 2026-08-27. Run the
pipeline on a different model and those numbers describe nothing, silently.

§9.3 of the master spec does say "text-embedding-004". ADR 0009 supersedes it with
measurements, and amendment **A3** records that override in the spec itself, so the two
documents no longer disagree. `text-embedding-004` stays the documented *fallback*.

Nothing here calls Vertex. Whether another model behaves identically is unverified and
must stay unverified until somebody measures it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jutsu_db import EMBEDDING_DIM
from jutsu_retrieval.config import DEFAULT_MODEL

REPO_ROOT = Path(__file__).resolve().parents[3]

#: ADR 0009's decision, and the only model any measurement in this package describes.
EXPECTED_MODEL = "gemini-embedding-001"

#: Named in ADR 0009 as available and 768-native, kept as the fallback. Never the default.
DOCUMENTED_FALLBACK = "text-embedding-004"


@pytest.fixture(scope="module")
def env_example() -> str:
    return (REPO_ROOT / ".env.example").read_text(encoding="utf-8")


class TestTheDefaultIsTheMeasuredModel:
    def test_the_code_default(self) -> None:
        assert DEFAULT_MODEL == EXPECTED_MODEL

    def test_the_committed_example_environment(self, env_example: str) -> None:
        assert f"EMBEDDING_MODEL={EXPECTED_MODEL}" in env_example

    def test_the_example_does_not_ship_the_fallback_as_the_default(self, env_example: str) -> None:
        assert f"EMBEDDING_MODEL={DOCUMENTED_FALLBACK}" not in env_example

    def test_the_width_the_column_already_has(self) -> None:
        """768 is not a preference. `chunks.embedding` is `vector(768)` with an HNSW
        index built on it, so anything else is a migration and a full re-index."""
        assert EMBEDDING_DIM == 768


class TestTheDocumentationAgrees:
    def test_adr_0009_records_the_decision(self) -> None:
        adr = REPO_ROOT / "docs" / "adr" / "0009-embedding-provider-and-vector-handling.md"
        text = adr.read_text(encoding="utf-8")
        assert "**Status:** accepted" in text
        assert EXPECTED_MODEL in text

    def test_the_spec_records_the_override_of_9_3(self) -> None:
        """Without amendment A3 the spec still reads `text-embedding-004` and a future
        reader has two documents that disagree and no way to tell which won."""
        spec = (REPO_ROOT / "docs" / "jutsu-master-spec.md").read_text(encoding="utf-8")
        amendments = spec.split("## 1 · Mission")[0]
        assert "A3" in amendments
        assert EXPECTED_MODEL in amendments

    def test_the_fallback_is_still_described_as_a_fallback(self) -> None:
        adr = REPO_ROOT / "docs" / "adr" / "0009-embedding-provider-and-vector-handling.md"
        assert "fallback" in adr.read_text(encoding="utf-8")


class TestTheRecordedFixtureMatchesTheDefault:
    def test_the_fixture_is_named_for_the_model_it_came_from(self) -> None:
        """The response fixture is a real capture. If the default model ever moves, this
        fails and names the thing that has to be re-captured rather than letting the old
        vectors quietly stand in for the new model's."""
        fixtures = Path(__file__).parent / "fixtures"
        expected = fixtures / f"{EXPECTED_MODEL.replace('-', '_')}_{EMBEDDING_DIM}.json"
        assert expected.is_file(), f"no recorded response for {EXPECTED_MODEL}"
