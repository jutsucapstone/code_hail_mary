"""The three outcomes, and what `--strict` does to the third.

These are the tests for the harness's own honesty. Everything else in this package
measures the product; this file measures the thing doing the measuring, which is the one
component whose failure mode is a green result rather than a red one.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from jutsu_evals.gate import Check, CheckResult, GateContext, GateReport, Outcome, run_phase

CLAUSE = "≥45k documents ingested"


def _report(*results: CheckResult) -> GateReport:
    from datetime import UTC, datetime

    return GateReport(phase=1, generated_at=datetime.now(UTC), results=results)


class TestCheckResultInvariant:
    """A result that could not look must not also report what it saw."""

    def test_not_measured_refuses_an_observation(self) -> None:
        with pytest.raises(ValueError, match="cannot carry an observation"):
            CheckResult("documents_ingested", CLAUSE, Outcome.NOT_MEASURED, "db down", observed=0)

    def test_not_measured_refuses_a_threshold(self) -> None:
        """The threshold is half of a comparison that never happened."""
        with pytest.raises(ValueError, match="cannot carry an observation"):
            CheckResult(
                "documents_ingested", CLAUSE, Outcome.NOT_MEASURED, "db down", threshold=45_000
            )

    def test_the_unmeasured_constructor_cannot_be_given_one(self) -> None:
        result = CheckResult.unmeasured("documents_ingested", CLAUSE, "database unreachable")
        assert result.observed is None
        assert result.threshold is None
        assert result.outcome is Outcome.NOT_MEASURED

    def test_a_measured_result_carries_both_sides_of_the_comparison(self) -> None:
        result = CheckResult.ok("documents_ingested", CLAUSE, "47k", observed=47_000, threshold=45)
        assert (result.observed, result.threshold) == (47_000, 45)

    def test_zero_is_a_legitimate_observation(self) -> None:
        """`0 == 0` passing must stay expressible.

        The invariant is about *unmeasured* results, not about falsy ones. Several
        clauses are satisfied by observing zero — zero unembedded chunks, zero PII
        detections — and an over-eager guard here would make them unreportable.
        """
        result = CheckResult.ok("chunks_embedded", CLAUSE, "all embedded", observed=0, threshold=0)
        assert result.observed == 0


class TestStrictPromotion:
    def test_unmeasured_passes_without_strict_and_fails_with_it(self) -> None:
        report = _report(
            CheckResult.ok("a", CLAUSE, "fine", observed=1, threshold=0),
            CheckResult.unmeasured("b", CLAUSE, "database unreachable"),
        )
        assert report.passed(strict=False) is True
        assert report.passed(strict=True) is False
        assert report.exit_code(strict=False) == 0
        assert report.exit_code(strict=True) == 1

    def test_a_failure_is_a_failure_either_way(self) -> None:
        report = _report(CheckResult.failure("a", CLAUSE, "0 documents", observed=0, threshold=45))
        assert report.passed(strict=False) is False
        assert report.passed(strict=True) is False

    def test_all_measured_and_green_passes_under_strict(self) -> None:
        report = _report(CheckResult.ok("a", CLAUSE, "fine", observed=1, threshold=0))
        assert report.passed(strict=True) is True

    def test_the_tally_partitions_the_results(self) -> None:
        report = _report(
            CheckResult.ok("a", CLAUSE, "fine", observed=1, threshold=0),
            CheckResult.failure("b", CLAUSE, "no", observed=0, threshold=1),
            CheckResult.unmeasured("c", CLAUSE, "database unreachable"),
        )
        assert (len(report.passes), len(report.failures), len(report.unmeasured)) == (1, 1, 1)
        assert len(report.results) == 3


class TestRunPhase:
    @pytest.fixture
    def ctx(self, tmp_path: Path) -> GateContext:
        return GateContext(repo_root=tmp_path, org_id=uuid.uuid4())

    async def test_a_skipped_check_is_unmeasured_not_passed(self, ctx: GateContext) -> None:
        """`--skip` must never look like a green clause."""

        async def never_runs(_: GateContext) -> CheckResult:  # pragma: no cover - not called
            raise AssertionError("a skipped check was executed")

        skipped = GateContext(repo_root=ctx.repo_root, skip=frozenset({"a"}))
        report = await run_phase(1, [Check("a", CLAUSE, never_runs)], skipped)

        assert report.results[0].outcome is Outcome.NOT_MEASURED
        assert "excluded by --skip" in report.results[0].detail
        assert report.passed(strict=True) is False

    async def test_a_crashing_check_is_unmeasured_not_failed(self, ctx: GateContext) -> None:
        """A harness fault is not a defect in the code under test."""

        async def explodes(_: GateContext) -> CheckResult:
            raise RuntimeError("connection reset")

        report = await run_phase(1, [Check("a", CLAUSE, explodes)], ctx)
        assert report.results[0].outcome is Outcome.NOT_MEASURED

    async def test_a_crash_reports_the_type_and_never_the_message(self, ctx: GateContext) -> None:
        """§4.9 — an exception message can carry a row, a query or an address."""

        async def leaks(_: GateContext) -> CheckResult:
            raise RuntimeError("no user found for kenneth.lay@enron.com")

        report = await run_phase(1, [Check("a", CLAUSE, leaks)], ctx)
        detail = report.results[0].detail
        assert "RuntimeError" in detail
        assert "enron.com" not in detail
        assert "kenneth" not in detail

    async def test_one_crash_does_not_stop_the_remaining_checks(self, ctx: GateContext) -> None:
        async def explodes(_: GateContext) -> CheckResult:
            raise RuntimeError("boom")

        async def fine(_: GateContext) -> CheckResult:
            return CheckResult.ok("b", CLAUSE, "fine", observed=1, threshold=0)

        report = await run_phase(1, [Check("a", CLAUSE, explodes), Check("b", CLAUSE, fine)], ctx)
        assert [r.outcome for r in report.results] == [Outcome.NOT_MEASURED, Outcome.PASSED]

    async def test_results_keep_the_order_the_clauses_are_stated_in(self, ctx: GateContext) -> None:
        async def make(name: str) -> Check:
            async def run(_: GateContext) -> CheckResult:
                return CheckResult.ok(name, CLAUSE, "fine", observed=1, threshold=0)

            return Check(name, CLAUSE, run)

        checks = [await make(n) for n in ("a", "b", "c")]
        report = await run_phase(1, checks, ctx)
        assert [r.name for r in report.results] == ["a", "b", "c"]

    async def test_the_report_serialises_to_json_safe_scalars(self, ctx: GateContext) -> None:
        import json

        async def fine(_: GateContext) -> CheckResult:
            return CheckResult.ok("a", CLAUSE, "fine", observed=1.5, threshold=70.0)

        report = await run_phase(1, [Check("a", CLAUSE, fine)], ctx, revision="abc1234")
        payload = json.loads(json.dumps(report.to_dict()))
        assert payload["revision"] == "abc1234"
        assert payload["summary"] == {"passed": 1, "failed": 0, "not_measured": 0}
        assert payload["results"][0]["clause"] == CLAUSE
