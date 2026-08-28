"""Run a phase gate and say what was measured (§21).

    uv run python scripts/gate.py --phase 1 --org <uuid>

Thin on purpose. Argument parsing, wiring and printing live here; every judgement lives
in `jutsu_evals`, where it can be tested without a process boundary. `scripts/emit-openapi.py`
draws the same line for the same reason.

**Exit 0 means the gate passed, not that the phase is done.** Without `--strict`, a clause
that could not be measured is reported as such and does not fail the run — the default
reader is a developer with the containers stopped. `--strict` is what a milestone
sign-off and CI use: there, an unmeasured clause is a failure, because a gate that signs
off on eleven clauses having looked at six is not a gate.

The one thing this script knows that the package does not is how to re-run ingestion.
`jutsu_evals` is a package and must not depend on `apps/worker`, so the idempotency check
takes the entry point as a callable and this file supplies the real one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from jutsu_evals import PHASES, GateContext, render_text, revision, run_phase, write_report

REPO_ROOT = Path(__file__).resolve().parent.parent


async def _reseed(org_id: uuid.UUID, root: str) -> int:
    """Re-run `make seed` for one corpus, in process.

    Imported lazily and inside the function so that a gate run which never reaches the
    idempotency check does not require the worker application to be importable at all.
    """
    from jutsu_worker.cli import seed

    # `embed=False`: the clause is about rows, and a gate that spent money to measure
    # idempotency would be a surprising thing to run twice.
    return await seed(org_id, root, embed=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jutsu-gate", description=__doc__)
    parser.add_argument("--phase", type=int, required=True, choices=sorted(PHASES))
    parser.add_argument("--org", help="organisation id — required by every tenant-scoped check")
    parser.add_argument(
        "--log",
        type=Path,
        help="a captured seed-run log to scan for raw PII",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the report as JSON instead of a table"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat a clause that could not be measured as a failure",
    )
    sample = parser.add_mutually_exclusive_group()
    sample.add_argument(
        "--sample",
        type=int,
        default=500,
        help="documents to replay in the offset check (default 500)",
    )
    sample.add_argument(
        "--full", action="store_true", help="replay every document's offsets, not a sample"
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        help="permit the idempotency check to re-run ingestion, which writes",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="CHECK",
        help="exclude a check by name; it is reported as not measured, never as passed",
    )
    parser.add_argument(
        "--no-write-report",
        action="store_true",
        help="do not write evals/reports/phase-N-<utc>.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Clause text and the report table carry `§` and `≥`. On a Windows console still in
    # its legacy code page those raise UnicodeEncodeError mid-render, which turns a
    # perfectly good gate result into a traceback.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ctx = GateContext(
        repo_root=REPO_ROOT,
        org_id=uuid.UUID(args.org) if args.org else None,
        log_path=args.log,
        sample=None if args.full else args.sample,
        allow_writes=args.allow_writes,
        reseed=_reseed,
        skip=frozenset(args.skip),
    )

    report = asyncio.run(
        run_phase(args.phase, PHASES[args.phase], ctx, revision=revision(REPO_ROOT))
    )

    if not args.no_write_report:
        path = write_report(REPO_ROOT, report)
    else:
        path = None

    if args.json:
        json.dump(report.to_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_text(report, strict=args.strict))
        if path is not None:
            sys.stdout.write(f"report written to {path.relative_to(REPO_ROOT)}\n")

    return report.exit_code(strict=args.strict)


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
