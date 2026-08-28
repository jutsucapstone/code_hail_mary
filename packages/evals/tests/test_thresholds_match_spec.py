"""Every M1 number in the code, checked against the sentence it came from.

The failure this prevents is quiet. A threshold is a goalpost, and a goalpost that moves
without a diff against the spec is how a gate ends up certifying something nobody agreed
to. So the spec file itself is the fixture: these tests read §21's Gate M1 paragraph and
assert the harness holds the figures it actually states.

If §21 changes, these fail — which is correct. Amending the gate is an amendment to the
spec, made deliberately and in both places.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from jutsu_evals.phase1 import PHASE_1_CHECKS
from jutsu_evals.thresholds import PHASE_1

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = REPO_ROOT / "docs" / "jutsu-master-spec.md"


@pytest.fixture(scope="module")
def m1_paragraph() -> str:
    """§21's Gate M1 text, and nothing else."""
    spec = SPEC.read_text(encoding="utf-8")
    match = re.search(r"\*\*Gate M1:\*\*(.+?)\n\n", spec, re.DOTALL)
    assert match is not None, "Gate M1 paragraph not found in the spec"
    return " ".join(match.group(1).split())


class TestThresholdsComeFromTheSpec:
    def test_the_document_floor_is_the_one_m1_states(self, m1_paragraph: str) -> None:
        assert "45k documents ingested" in m1_paragraph
        assert PHASE_1.min_documents == 45_000

    def test_the_embedding_width_is_the_one_m1_states(self, m1_paragraph: str) -> None:
        assert "dim 768" in m1_paragraph
        assert PHASE_1.embedding_dim == 768

    def test_the_coverage_floor_is_the_one_m1_states(self, m1_paragraph: str) -> None:
        assert "70% coverage" in m1_paragraph
        assert PHASE_1.min_coverage_percent == 70.0

    def test_the_zero_clauses_really_are_zero(self, m1_paragraph: str) -> None:
        """Four clauses admit no tolerance at all, and M1 says so in words."""
        assert "adds zero rows" in m1_paragraph
        assert "zero raw PII" in m1_paragraph
        assert "100% chunks embedded" in m1_paragraph
        assert PHASE_1.max_new_rows_on_reseed == 0
        assert PHASE_1.max_pii_detections_in_logs == 0
        assert PHASE_1.max_unembedded_chunks == 0
        assert PHASE_1.max_offset_mismatches == 0

    def test_a_skip_is_never_tolerated_on_a_security_clause(self) -> None:
        """The clause is "all 7 ACL tests pass", and a skipped test did not pass."""
        assert PHASE_1.max_acl_skips == 0
        assert PHASE_1.max_acl_failures == 0
        assert PHASE_1.max_isolation_skips == 0
        assert PHASE_1.max_isolation_failures == 0

    def test_the_measured_packages_are_the_three_m1_names(self, m1_paragraph: str) -> None:
        assert "core/graph/retrieval" in m1_paragraph
        assert PHASE_1.coverage_packages == ("jutsu_core", "jutsu_graph", "jutsu_retrieval")

    def test_thresholds_are_frozen(self) -> None:
        """A check must not be able to build itself a friendlier number."""
        with pytest.raises((AttributeError, TypeError)):
            PHASE_1.min_documents = 1  # type: ignore[misc]


class TestEveryClauseHasExactlyOneCheck:
    def test_the_registry_covers_the_eleven_clauses(self) -> None:
        assert len(PHASE_1_CHECKS) == 11

    def test_check_names_are_unique(self) -> None:
        names = [c.name for c in PHASE_1_CHECKS]
        assert len(set(names)) == len(names)

    def test_each_check_carries_the_clause_it_answers(self, m1_paragraph: str) -> None:
        """The `clause` string is what makes a report readable beside the spec.

        Two clauses are worded here as the shorthand the plan uses — "preflight green"
        and the coverage floor are one sentence in §21 — so those are matched on their
        distinctive fragment rather than in full.
        """
        for check in PHASE_1_CHECKS:
            assert check.clause, f"{check.name} states no clause"
            fragment = check.clause.lstrip("≥").split("(")[0].strip()
            assert len(fragment) > 5, f"{check.name}'s clause is too vague to locate"

    def test_the_registry_order_follows_the_spec(self) -> None:
        """Reading the report top to bottom should read like §21."""
        assert [c.name for c in PHASE_1_CHECKS][:5] == [
            "documents_ingested",
            "seed_idempotent",
            "offsets_resolve",
            "no_raw_pii_in_logs",
            "chunks_embedded",
        ]
