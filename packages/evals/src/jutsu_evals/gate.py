"""What a gate is allowed to say (§21, CLAUDE.md rule 8).

A gate has **three** answers, not two. `passed` and `failed` are the obvious pair; the
third is `not_measured`, and it is the whole reason this module exists as types rather
than as a script that prints booleans.

Rule 8 is "never invent numbers". The way that rule gets broken is not by fabrication —
nobody types a made-up figure — it is by a check that could not run reporting the value
of a variable that was never assigned. A corpus that was never ingested makes
`count(*)` return 0, and `0 < 45000` is a perfectly well-formed failure that means
nothing at all. Reported as a failure it teaches the reader that the gate is noisy;
reported as *not measured* it says exactly what happened.

So `CheckResult` refuses to hold an observation it did not make: constructing a
`not_measured` result with an `observed` value raises. That is rule 8 expressed as an
invariant instead of as a paragraph nobody re-reads.

`--strict` promotes `not_measured` to failure, which is what CI wants and what a
milestone sign-off wants. It is deliberately not the default, because the default reader
is a developer on a laptop with the containers stopped, and telling them their gate
failed would be a lie about their code.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "Check",
    "CheckFn",
    "CheckResult",
    "GateContext",
    "GateReport",
    "Observed",
    "Outcome",
    "ReseedFn",
    "run_phase",
]


class Outcome(StrEnum):
    """The three things a check may conclude."""

    PASSED = "passed"
    FAILED = "failed"
    #: The check could not be run at all. Carries a reason and never a number.
    NOT_MEASURED = "not_measured"


#: What a check observed. Deliberately narrow — a gate reports scalars, not structures,
#: because anything richer ends up carrying document content into a report file.
Observed = int | float | str | None


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One M1 clause, and what looking at it actually found.

    `clause` is the sentence from §21 that this check exists to answer, carried
    alongside the machine name so a report can be read next to the spec without
    anybody having to remember the mapping.
    """

    name: str
    clause: str
    outcome: Outcome
    detail: str
    observed: Observed = None
    threshold: Observed = None

    def __post_init__(self) -> None:
        # The invariant this module is for. A result that says "I could not look" must
        # not also say "and here is what I saw".
        if self.outcome is Outcome.NOT_MEASURED and (
            self.observed is not None or self.threshold is not None
        ):
            raise ValueError(f"check {self.name!r} is not_measured and cannot carry an observation")

    @staticmethod
    def ok(
        name: str,
        clause: str,
        detail: str,
        *,
        observed: Observed = None,
        threshold: Observed = None,
    ) -> CheckResult:
        return CheckResult(name, clause, Outcome.PASSED, detail, observed, threshold)

    @staticmethod
    def failure(
        name: str,
        clause: str,
        detail: str,
        *,
        observed: Observed = None,
        threshold: Observed = None,
    ) -> CheckResult:
        return CheckResult(name, clause, Outcome.FAILED, detail, observed, threshold)

    @staticmethod
    def unmeasured(name: str, clause: str, reason: str) -> CheckResult:
        """The check did not run. `reason` says why, and no number is reported."""
        return CheckResult(name, clause, Outcome.NOT_MEASURED, reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "clause": self.clause,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "observed": self.observed,
            "threshold": self.threshold,
        }


#: Re-run one source's ingestion, returning how many documents it handled.
#:
#: Injected rather than imported. The idempotency clause is the only M1 clause whose
#: measurement is itself a *write*, and the code that performs it lives in `apps/worker`
#: — an application, which a package must not depend on. The CLI wires the real one in;
#: a test wires in a fake and still exercises the comparison.
ReseedFn = Callable[[uuid.UUID, str], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class GateContext:
    """Everything a check is allowed to know about the run.

    No database session and no engine: each check opens its own `org_session`, so a
    check that forgets to scope itself sees nothing rather than inheriting a scope from
    the harness. The gate measures the application's view of the data, through the
    application's role, or it is not measuring the application.
    """

    repo_root: Path
    org_id: uuid.UUID | None = None
    #: A captured seed-run log to scan for PII. Passed on the command line rather than
    #: read out of the run receipt, so no receipt ever has to store a filesystem path.
    log_path: Path | None = None
    #: How many documents the offset check replays. `None` means every one of them.
    sample: int | None = 500
    #: Fixed so two runs over the same corpus examine the same documents. A sampled
    #: check whose sample moves cannot be compared with its previous result.
    seed: int = 20260819
    #: Required by the one check that writes. Absent, that check is not measured.
    allow_writes: bool = False
    reseed: ReseedFn | None = None
    skip: frozenset[str] = field(default_factory=frozenset)


CheckFn = Callable[[GateContext], Awaitable[CheckResult]]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    #: The §21 clause this check answers, quoted closely enough to find in the spec.
    clause: str
    run: CheckFn


@dataclass(frozen=True, slots=True)
class GateReport:
    phase: int
    generated_at: datetime
    results: tuple[CheckResult, ...]
    #: Git revision the gate ran against, so a milestone number names a commit (§18).
    revision: str | None = None

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.outcome is Outcome.FAILED)

    @property
    def unmeasured(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.outcome is Outcome.NOT_MEASURED)

    @property
    def passes(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.outcome is Outcome.PASSED)

    def passed(self, *, strict: bool) -> bool:
        """Whether the gate is green.

        Under `--strict` an unmeasured clause is a failure, because a milestone that
        signs off on eleven clauses having measured six is not a milestone.
        """
        if self.failures:
            return False
        return not (strict and self.unmeasured)

    def exit_code(self, *, strict: bool) -> int:
        return 0 if self.passed(strict=strict) else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "generated_at": self.generated_at.isoformat(),
            "revision": self.revision,
            "summary": {
                "passed": len(self.passes),
                "failed": len(self.failures),
                "not_measured": len(self.unmeasured),
            },
            "results": [r.to_dict() for r in self.results],
        }


async def run_phase(
    phase: int, checks: Sequence[Check], ctx: GateContext, *, revision: str | None = None
) -> GateReport:
    """Run every check in order and collect the results.

    Checks run sequentially, not concurrently. Several of them run a full test suite or
    a migration round trip, and two of those competing for the same database would make
    each other's results meaningless — a gate that is fast and wrong is the one failure
    mode worth designing against here.

    **An unexpected exception is `not_measured`, never `failed`.** A check that crashed
    did not observe anything, so saying it failed would attribute a defect to the code
    under test rather than to the harness. Only the exception *type* is recorded: an
    exception message can carry a row, a query or an address, and §4.9 applies to a gate
    report exactly as it applies to a log line.
    """
    results: list[CheckResult] = []

    for check in checks:
        if check.name in ctx.skip:
            results.append(CheckResult.unmeasured(check.name, check.clause, "excluded by --skip"))
            continue
        try:
            results.append(await check.run(ctx))
        except Exception as exc:
            results.append(
                CheckResult.unmeasured(
                    check.name, check.clause, f"check raised {type(exc).__name__}"
                )
            )

    return GateReport(
        phase=phase,
        generated_at=datetime.now(UTC),
        results=tuple(results),
        revision=revision,
    )
