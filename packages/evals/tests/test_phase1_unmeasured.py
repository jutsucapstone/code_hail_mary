"""Every path by which a clause goes unmeasured, and what it says when it does.

These tests are the reason the harness is worth having. Each one puts a check in a
situation where it genuinely cannot answer, and asserts that it says so — rather than
returning the value of a query that found nothing, or a suite that ran nothing.

They need no services. That is deliberate: the unmeasured paths are exactly the ones a
developer with the containers stopped will hit, so they must be testable in the same
state.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from jutsu_evals.gate import Check, CheckResult, GateContext, Outcome, run_phase
from jutsu_evals.phase1 import (
    PHASE_1_CHECKS,
    REVERSIBILITY_SUITE,
    REVERSIBILITY_TEST,
    _PytestRun,
    _suite_result,
    check_documents_ingested,
    check_no_raw_pii_in_logs,
    check_seed_idempotent,
    check_token_cost_recorded,
)

#: The checks that cannot answer anything without knowing whose data to look at.
ORG_SCOPED = (
    "documents_ingested",
    "seed_idempotent",
    "offsets_resolve",
    "chunks_embedded",
    "token_cost_recorded",
)

DB_DOWN = os.environ.get("JUTSU_DB_REACHABLE") != "1"


def _ctx(tmp_path: Path, **overrides: object) -> GateContext:
    return GateContext(repo_root=tmp_path, **overrides)  # type: ignore[arg-type]


class TestWithoutAnOrganisation:
    @pytest.mark.parametrize(
        "check", [c for c in PHASE_1_CHECKS if c.name in ORG_SCOPED], ids=lambda c: c.name
    )
    async def test_a_tenant_scoped_clause_is_unmeasured(self, check: Check, tmp_path: Path) -> None:
        result = await check.run(_ctx(tmp_path))
        assert result.outcome is Outcome.NOT_MEASURED
        assert "--org" in result.detail

    async def test_it_does_not_report_zero_documents(self, tmp_path: Path) -> None:
        """The failure this whole design exists to prevent.

        `count(*)` over a database nobody named returns nothing, and `0 < 45000` is a
        well-formed failure that means nothing at all.
        """
        result = await check_documents_ingested(_ctx(tmp_path))
        assert result.outcome is Outcome.NOT_MEASURED
        assert result.observed is None
        assert "0" not in result.detail


class TestTheOnlyCheckThatWrites:
    async def test_it_refuses_without_allow_writes(self, tmp_path: Path) -> None:
        result = await check_seed_idempotent(_ctx(tmp_path, org_id=uuid.uuid4()))
        assert result.outcome is Outcome.NOT_MEASURED
        assert "--allow-writes" in result.detail

    async def test_it_refuses_without_an_ingestion_entry_point(self, tmp_path: Path) -> None:
        """`--allow-writes` alone is not enough; something has to do the writing."""
        result = await check_seed_idempotent(
            _ctx(tmp_path, org_id=uuid.uuid4(), allow_writes=True, reseed=None)
        )
        assert result.outcome is Outcome.NOT_MEASURED
        assert "ingestion entry point" in result.detail

    async def test_it_does_not_call_the_reseed_function_when_refusing(self, tmp_path: Path) -> None:
        called = False

        async def reseed(org_id: uuid.UUID, root: str) -> int:
            nonlocal called
            called = True
            return 0

        await check_seed_idempotent(_ctx(tmp_path, org_id=uuid.uuid4(), reseed=reseed))
        assert called is False, "the check wrote to the database without permission"


class TestTheLogScan:
    async def test_no_log_means_unmeasured(self, tmp_path: Path) -> None:
        result = await check_no_raw_pii_in_logs(_ctx(tmp_path))
        assert result.outcome is Outcome.NOT_MEASURED
        assert "--log" in result.detail

    async def test_a_missing_file_means_unmeasured(self, tmp_path: Path) -> None:
        result = await check_no_raw_pii_in_logs(_ctx(tmp_path, log_path=tmp_path / "absent.log"))
        assert result.outcome is Outcome.NOT_MEASURED

    async def test_a_clean_log_passes(self, tmp_path: Path) -> None:
        log = tmp_path / "seed.log"
        log.write_text(
            "document_created org=8f14e45f source=1c383cd3 document=aab32389 "
            "chunks=12 grants=3\nseed_completed org=8f14e45f documents=2\n",
            encoding="utf-8",
        )
        result = await check_no_raw_pii_in_logs(_ctx(tmp_path, log_path=log))
        assert result.outcome is Outcome.PASSED
        assert result.observed == 0

    async def test_a_leaking_log_fails(self, tmp_path: Path) -> None:
        log = tmp_path / "seed.log"
        log.write_text(
            "document_created org=8f14e45f author=kenneth.lay@enron.example\n"
            "contact +1 713 555 0142\n",
            encoding="utf-8",
        )
        result = await check_no_raw_pii_in_logs(_ctx(tmp_path, log_path=log))
        assert result.outcome is Outcome.FAILED
        assert isinstance(result.observed, int)
        assert result.observed >= 2

    async def test_the_failure_names_types_and_never_values(self, tmp_path: Path) -> None:
        """A check that printed the PII it found would have created the leak."""
        log = tmp_path / "seed.log"
        log.write_text("author=kenneth.lay@enron.example phone=+1 713 555 0142\n", encoding="utf-8")
        result = await check_no_raw_pii_in_logs(_ctx(tmp_path, log_path=log))

        assert "kenneth.lay@enron.example" not in result.detail
        assert "713 555 0142" not in result.detail
        assert "email" in result.detail

    async def test_masked_pseudonyms_are_not_mistaken_for_leaks(self, tmp_path: Path) -> None:
        """`[EMAIL_A7]` in a log is the masking working, not a finding."""
        log = tmp_path / "seed.log"
        log.write_text("chunk text preview [EMAIL_A7] and [PHONE_B2]\n", encoding="utf-8")
        result = await check_no_raw_pii_in_logs(_ctx(tmp_path, log_path=log))
        assert result.outcome is Outcome.PASSED


class TestTheCostClause:
    async def test_no_receipt_means_unmeasured(self, tmp_path: Path) -> None:
        result = await check_token_cost_recorded(_ctx(tmp_path, org_id=uuid.uuid4()))
        assert result.outcome is Outcome.NOT_MEASURED
        assert "evals/runs" in result.detail

    async def test_a_receipt_from_a_run_without_embedding_still_counts(
        self, tmp_path: Path
    ) -> None:
        from datetime import UTC, datetime

        from jutsu_evals.receipts import SeedReceipt, write_receipt

        org_id = uuid.uuid4()
        write_receipt(
            tmp_path,
            SeedReceipt.build(
                org_id=org_id,
                root="/corpus",
                started_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
                finished_at=datetime(2026, 8, 28, 12, 1, tzinfo=UTC),
                documents=2,
                chunks=6,
                embedded=False,
                tokens=0,
                requests=0,
                model=None,
                dimension=None,
            ),
        )
        result = await check_token_cost_recorded(_ctx(tmp_path, org_id=org_id))
        assert result.outcome is Outcome.PASSED
        assert result.observed == 0


@pytest.mark.skipif(not DB_DOWN, reason="the database is up, so these paths cannot be reached")
class TestWithoutADatabase:
    """What a developer with the containers stopped actually sees."""

    @pytest.mark.parametrize(
        "check",
        [c for c in PHASE_1_CHECKS if c.name in {"documents_ingested", "chunks_embedded"}],
        ids=lambda c: c.name,
    )
    async def test_an_unreachable_database_is_unmeasured(
        self, check: Check, tmp_path: Path
    ) -> None:
        result = await check.run(_ctx(tmp_path, org_id=uuid.uuid4()))
        assert result.outcome is Outcome.NOT_MEASURED
        assert "unreachable" in result.detail
        assert result.observed is None


class TestTheWholeRunUnderStrict:
    async def test_a_run_that_measured_nothing_is_not_a_pass(self, tmp_path: Path) -> None:
        """The headline claim: S9 building the harness is not an M1 pass."""
        ctx = GateContext(
            repo_root=tmp_path,
            skip=frozenset(c.name for c in PHASE_1_CHECKS),
        )
        report = await run_phase(1, PHASE_1_CHECKS, ctx)

        assert len(report.unmeasured) == 11
        assert report.passed(strict=True) is False
        assert all(r.observed is None for r in report.results)

    async def test_no_unmeasured_result_anywhere_carries_a_number(self, tmp_path: Path) -> None:
        report = await run_phase(
            1,
            PHASE_1_CHECKS,
            GateContext(
                repo_root=tmp_path,
                skip=frozenset(
                    {
                        "acl_suite",
                        "org_isolation",
                        "migrations_reversible",
                        "preflight",
                        "coverage_core",
                    }
                ),
            ),
        )
        for result in report.results:
            if result.outcome is Outcome.NOT_MEASURED:
                assert result.observed is None
                assert result.threshold is None


class TestUnmeasuredIsNotFailed:
    def test_the_two_are_distinguishable_in_the_serialised_report(self) -> None:
        failed = CheckResult.failure("a", "clause", "no", observed=0, threshold=1).to_dict()
        unmeasured = CheckResult.unmeasured("a", "clause", "database unreachable").to_dict()
        assert failed["outcome"] == "failed"
        assert unmeasured["outcome"] == "not_measured"
        assert unmeasured["observed"] is None


class TestSuiteVerdicts:
    """`_suite_result` — the rule that a skip is neither a pass nor a failure.

    Exercised directly rather than through a subprocess, because the interesting cases
    are the ones a developer's machine produces only some of the time.
    """

    def test_all_green_passes(self) -> None:
        result = _suite_result(
            "acl_suite",
            "clause",
            _PytestRun(0, 39, 0, 0, 0),
            max_failures=0,
            max_skips=0,
            what="the ACL suite",
        )
        assert result.outcome is Outcome.PASSED
        assert "39 tests passed" in result.detail

    def test_a_failure_is_red(self) -> None:
        result = _suite_result(
            "acl_suite",
            "clause",
            _PytestRun(1, 39, 2, 0, 0),
            max_failures=0,
            max_skips=0,
            what="the ACL suite",
        )
        assert result.outcome is Outcome.FAILED
        assert result.observed == 2

    def test_a_partial_skip_is_unmeasured_not_passed(self) -> None:
        """36 of 39 skipping is the local reality with the containers stopped."""
        result = _suite_result(
            "acl_suite",
            "clause",
            _PytestRun(0, 39, 0, 0, 36),
            max_failures=0,
            max_skips=0,
            what="the ACL suite",
        )
        assert result.outcome is Outcome.NOT_MEASURED
        assert "36 of 39" in result.detail

    def test_an_error_counts_as_a_failure(self) -> None:
        result = _suite_result(
            "acl_suite",
            "clause",
            _PytestRun(1, 39, 0, 3, 0),
            max_failures=0,
            max_skips=0,
            what="the ACL suite",
        )
        assert result.outcome is Outcome.FAILED

    def test_collecting_nothing_is_unmeasured(self) -> None:
        """A node id that stopped matching must not read as a clean run."""
        result = _suite_result(
            "migrations_reversible",
            "clause",
            _PytestRun(5, 0, 0, 0, 0),
            max_failures=0,
            max_skips=0,
            what="the migration round trip",
        )
        assert result.outcome is Outcome.NOT_MEASURED
        assert "collected no tests" in result.detail


class TestTheReversibilitySelector:
    def test_the_test_is_selected_by_name_not_by_node_id(self) -> None:
        """A node id would have to name the class, which this module does not own.

        The first implementation used one, it did not match, and the clause reported
        "collected no tests" — unmeasured, which is honest but useless.
        """
        assert "::" not in REVERSIBILITY_TEST
        source = (Path(__file__).resolve().parents[3] / REVERSIBILITY_SUITE).read_text(
            encoding="utf-8"
        )
        assert f"def {REVERSIBILITY_TEST}" in source, "the selected test no longer exists"
