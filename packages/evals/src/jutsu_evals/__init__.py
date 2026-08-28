"""JUTSU evals — the harness that produces every number this project quotes.

CLAUDE.md rule 8 says metrics come from `make eval` and latency from traces, and that
anything you cannot measure you say you cannot measure. This package is the first half
of that sentence: `gate` defines what a check may conclude, `phase1` implements Gate M1's
eleven clauses, `report` writes the result down next to the commit it describes.

Phase 2's extraction eval (S14) and Phase 3's benchmark (S23) extend the same registry.
"""

from jutsu_evals.gate import (
    Check,
    CheckResult,
    GateContext,
    GateReport,
    Outcome,
    ReseedFn,
    run_phase,
)
from jutsu_evals.phase1 import PHASE_1_CHECKS
from jutsu_evals.receipts import SeedReceipt, latest_receipt, write_receipt
from jutsu_evals.report import render_text, revision, write_report
from jutsu_evals.thresholds import PHASE_1, Phase1Thresholds

#: Phase number to its checks. S14 and S23 add entries; nothing else changes.
PHASES: dict[int, tuple[Check, ...]] = {1: PHASE_1_CHECKS}

__all__ = [
    "PHASES",
    "PHASE_1",
    "PHASE_1_CHECKS",
    "Check",
    "CheckResult",
    "GateContext",
    "GateReport",
    "Outcome",
    "Phase1Thresholds",
    "ReseedFn",
    "SeedReceipt",
    "latest_receipt",
    "render_text",
    "revision",
    "run_phase",
    "write_receipt",
    "write_report",
]
