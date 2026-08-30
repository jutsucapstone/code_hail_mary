"""Rendering a gate report, for a terminal and for the record (§18).

Two audiences. The terminal wants to know what to do next, so failures and unmeasured
clauses carry their reason on the same line. The JSON file is the record: §18 says a
regressing metric must name the commit, which it can only do if the number and the
revision were written down together at the moment of measurement.

Reports are committed, unlike run receipts. A report holds check names, outcomes,
scalars and a git revision — no paths, no corpus, no principals — so it is safe to keep,
and a milestone with no evidence behind it is the thing rule 8 exists to prevent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from jutsu_evals.gate import GateReport, Outcome

__all__ = ["render_text", "report_path", "revision", "write_report"]

_MARKERS = {
    Outcome.PASSED: "PASS",
    Outcome.FAILED: "FAIL",
    Outcome.NOT_MEASURED: "----",
}


def _git(repo_root: Path, *args: str) -> str | None:
    """Run one git command in `repo_root`, or None if git is unavailable or fails."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - resolved binary, fixed argv, no shell
            [git, *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def revision(repo_root: Path) -> str | None:
    """Short git revision, suffixed `-dirty` when tracked files differ from it.

    Best effort by design: a gate that refused to run because it could not find git
    would be failing over its own provenance stamp.

    **The suffix is the load-bearing half.** A report stamped `53a0f1c` over a run whose
    code was uncommitted claims a reproducibility it does not have — checking that commit
    out does not reproduce the measurement. Rule 8 asks a number to name its provenance,
    and naming the wrong one is worse than naming none: the reader cannot tell, and the
    report looks *more* trustworthy for having a commit on it. Measured on this repo:
    the retry, PII and drain-loop fixes all sat uncommitted while the gate reported a
    commit that contained none of them.

    Only **tracked** modifications set it. Untracked files are excluded deliberately —
    local tooling, receipts and scratch directories live in every working copy, and a
    flag that is always on is one nobody reads. The cost is a new, never-added source
    file: it changes the run and does not show here. `git status` does.
    """
    head = _git(repo_root, "rev-parse", "--short", "HEAD")
    if head is None or not head.strip():
        return None
    short = head.strip()

    # `--untracked-files=no`: see the docstring. Empty output means the tracked tree
    # matches HEAD; anything at all means it does not.
    status = _git(repo_root, "status", "--porcelain", "--untracked-files=no")
    if status is None:
        # HEAD is known but cleanliness is not, and silently implying "clean" is the
        # failure this function exists to prevent.
        return f"{short}-unknown"
    return f"{short}-dirty" if status.strip() else short


def render_text(report: GateReport, *, strict: bool) -> str:
    """The terminal view: one line per clause, then the tally."""
    width = max((len(r.name) for r in report.results), default=0)
    lines = [
        f"JUTSU gate — phase {report.phase}",
        f"generated {report.generated_at.isoformat()}"
        + (f" · revision {report.revision}" if report.revision else ""),
        "",
    ]

    for result in report.results:
        marker = _MARKERS[result.outcome]
        lines.append(f"  {marker}  {result.name.ljust(width)}  {result.detail}")

    lines.extend(
        [
            "",
            f"{len(report.passes)} passed · {len(report.failures)} failed · "
            f"{len(report.unmeasured)} not measured",
        ]
    )

    # Failures first, then unmeasured, and only then "passed" — the order matters more
    # than it looks. Asking `report.passed(strict=False)` first would print "gate PASSED"
    # over a run where nothing was measured at all, which is precisely the sentence this
    # whole harness exists to make unsayable.
    if report.failures:
        lines.append("gate FAILED — a measured clause did not hold")
    elif report.unmeasured:
        lines.append(
            f"gate NOT PASSED — nothing measured has failed, but "
            f"{len(report.unmeasured)} of {len(report.results)} clauses were never "
            f"measured. This is not an M1 pass."
        )
        if not strict:
            lines.append("(exit 0 because --strict was not given; --strict fails on these)")
    else:
        lines.append(f"gate PASSED — all {len(report.results)} clauses measured and green")

    return "\n".join(lines) + "\n"


def report_path(repo_root: Path, report: GateReport) -> Path:
    """`evals/reports/phase-N-<utc>.json`, with colons stripped for Windows."""
    stamp = report.generated_at.isoformat().replace(":", "").replace("+0000", "Z")
    return repo_root / "evals" / "reports" / f"phase-{report.phase}-{stamp}.json"


def write_report(repo_root: Path, report: GateReport) -> Path:
    path = report_path(repo_root, report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path
