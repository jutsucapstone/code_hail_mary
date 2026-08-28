"""What the report says, and the one sentence it must never say.

The rendering is not cosmetic. A gate whose summary line reads "gate PASSED" over a run
that measured nothing is worse than no gate: it converts an absence of evidence into a
claim, which is the exact failure CLAUDE.md rule 8 is about.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jutsu_evals.gate import CheckResult, GateReport
from jutsu_evals.report import render_text, report_path, write_report

CLAUSE = "≥45k documents ingested"


def _report(*results: CheckResult, revision: str | None = "e5f8bdd") -> GateReport:
    return GateReport(
        phase=1,
        generated_at=datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC),
        results=results,
        revision=revision,
    )


class TestTheSummaryLine:
    def test_a_run_with_unmeasured_clauses_never_claims_a_pass(self) -> None:
        rendered = render_text(
            _report(
                CheckResult.ok("a", CLAUSE, "fine", observed=1, threshold=0),
                CheckResult.unmeasured("b", CLAUSE, "database unreachable"),
            ),
            strict=False,
        )
        assert "gate PASSED" not in rendered
        assert "NOT PASSED" in rendered
        assert "not an M1 pass" in rendered

    def test_a_run_that_measured_nothing_never_claims_a_pass(self) -> None:
        """Eleven unmeasured clauses and zero failures is the default local state."""
        rendered = render_text(
            _report(*(CheckResult.unmeasured(f"c{i}", CLAUSE, "no --org") for i in range(11))),
            strict=False,
        )
        assert "gate PASSED" not in rendered
        assert "11 of 11 clauses were never" in rendered

    def test_a_measured_failure_says_failed(self) -> None:
        rendered = render_text(
            _report(CheckResult.failure("a", CLAUSE, "0 documents", observed=0, threshold=45_000)),
            strict=False,
        )
        assert "gate FAILED" in rendered

    def test_only_an_all_measured_green_run_says_passed(self) -> None:
        rendered = render_text(
            _report(CheckResult.ok("a", CLAUSE, "fine", observed=1, threshold=0)), strict=True
        )
        assert "gate PASSED" in rendered
        assert "all 1 clauses measured and green" in rendered

    def test_the_non_strict_exit_code_is_explained_rather_than_hidden(self) -> None:
        rendered = render_text(
            _report(CheckResult.unmeasured("a", CLAUSE, "no --org")), strict=False
        )
        assert "--strict" in rendered


class TestTheTable:
    def test_every_outcome_has_a_distinct_marker(self) -> None:
        rendered = render_text(
            _report(
                CheckResult.ok("passing", CLAUSE, "fine", observed=1, threshold=0),
                CheckResult.failure("failing", CLAUSE, "no", observed=0, threshold=1),
                CheckResult.unmeasured("absent", CLAUSE, "database unreachable"),
            ),
            strict=False,
        )
        assert "PASS  passing" in rendered
        assert "FAIL  failing" in rendered
        assert "----  absent" in rendered

    def test_the_revision_is_shown_so_a_number_names_a_commit(self) -> None:
        assert "e5f8bdd" in render_text(_report(), strict=False)

    def test_a_missing_revision_is_simply_absent(self) -> None:
        rendered = render_text(_report(revision=None), strict=False)
        assert "revision" not in rendered


class TestTheWrittenReport:
    def test_it_lands_where_the_slice_says(self, tmp_path: Path) -> None:
        path = write_report(tmp_path, _report())
        assert path.parent == tmp_path / "evals" / "reports"
        assert path.name.startswith("phase-1-")
        assert path.suffix == ".json"

    def test_the_filename_has_no_colon(self, tmp_path: Path) -> None:
        assert ":" not in report_path(tmp_path, _report()).name

    def test_it_round_trips_as_json(self, tmp_path: Path) -> None:
        report = _report(
            CheckResult.ok("a", CLAUSE, "fine", observed=47_102, threshold=45_000),
            CheckResult.unmeasured("b", CLAUSE, "database unreachable"),
        )
        payload = json.loads(write_report(tmp_path, report).read_text(encoding="utf-8"))

        assert payload["phase"] == 1
        assert payload["revision"] == "e5f8bdd"
        assert payload["summary"] == {"passed": 1, "failed": 0, "not_measured": 1}
        assert payload["results"][0]["observed"] == 47_102
        assert payload["results"][1]["observed"] is None

    def test_it_carries_the_clause_so_it_can_be_read_beside_the_spec(self, tmp_path: Path) -> None:
        report = _report(CheckResult.ok("a", CLAUSE, "fine", observed=1, threshold=0))
        payload = json.loads(write_report(tmp_path, report).read_text(encoding="utf-8"))
        assert payload["results"][0]["clause"] == CLAUSE

    def test_it_holds_no_filesystem_path(self, tmp_path: Path) -> None:
        """Reports are committed, so they must contain nothing local."""
        report = _report(CheckResult.ok("a", CLAUSE, "fine", observed=1, threshold=0))
        written = write_report(tmp_path, report).read_text(encoding="utf-8")
        assert str(tmp_path) not in written
