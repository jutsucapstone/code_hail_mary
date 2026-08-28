"""Gate M1, clause by clause (§21).

Eleven checks, each answering one sentence of the M1 paragraph and nothing else. The
mapping is carried on every result as `clause`, so a report can be read beside the spec
without anybody holding the correspondence in their head.

Three rules run through all of them.

**Measure through the application's eyes.** Every database check goes through
`org_session`, which means the restricted `jutsu_app` role and the RLS policies that role
is subject to. A gate that measured through `MIGRATION_DATABASE_URL` would be describing a
database the application cannot see, and ADR 0003 exists because that mistake leaves every
isolation test still passing.

**A skip is not a pass, and it is not a failure either.** Where a check is backed by a
test suite that skipped — the containers are down, the test database is not configured —
the clause was not proven, so the result is `not_measured` naming how many tests did not
run. Calling it a failure would blame the code for the harness's circumstances; calling it
a pass is the bug that made `conftest.py` probe reachability in the first place.

**Only one check writes, and it asks.** `seed_idempotent` re-runs ingestion, because
"a second `make seed` adds zero rows" cannot be observed without a second `make seed`. It
requires `--allow-writes` and is otherwise not measured.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from jutsu_core import MaskedSpan
from jutsu_core.pii import mask
from jutsu_db.engine import org_session, ping
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_evals.gate import Check, CheckResult, GateContext
from jutsu_evals.receipts import latest_receipt
from jutsu_evals.thresholds import PHASE_1

__all__ = ["PHASE_1_CHECKS"]

#: How long any one subprocess check may take. Generous — `preflight` runs the web lint,
#: both type checkers and the whole Python suite — and bounded, because a gate that hangs
#: is a gate nobody runs.
_SUBPROCESS_TIMEOUT = 3600

#: Documents fetched per round trip in the offset replay. Bodies are large; pulling
#: 45 000 of them into one result set is how a check becomes the reason a gate is skipped.
_OFFSET_BATCH = 50


# --------------------------------------------------------------------- shared helpers


async def _database_unavailable() -> str | None:
    """Why the database cannot be measured, or None when it can.

    Two reasons, kept apart because they call for opposite actions and the first
    masquerading as the second wasted a forty-minute gate run: `scripts/gate.py` was
    invoked without `--env-file`, so `DATABASE_URL` was unset, `get_engine()` raised
    inside `ping`'s except clause, and three clauses reported "database unreachable"
    about a Postgres that was up and healthy. A reason that names the wrong cause is
    worse than no reason — it sends the reader to restart a container that is running.

    `ping` opens an unscoped session, which is the one legitimate use of it here: this
    asks about the connection, not about any tenant's data.
    """
    if not os.environ.get("DATABASE_URL"):
        return (
            "DATABASE_URL is not set for this process — run via `make eval` / `make gate`, "
            "which pass --env-file"
        )
    if not await ping():
        return "database unreachable"
    return None


def _needs_org(ctx: GateContext) -> str | None:
    if ctx.org_id is None:
        return "no --org was given, and every tenant-scoped measurement needs one"
    return None


@dataclass(frozen=True, slots=True)
class _PytestRun:
    """A pytest invocation's counts, taken from its own JUnit report."""

    exit_code: int
    tests: int
    failures: int
    errors: int
    skipped: int
    #: One entry per skipped test, deduplicated. Carried because "how many skipped" is
    #: not enough to decide whether a coverage figure is meaningful — *why* is.
    skip_reasons: tuple[str, ...] = ()

    @property
    def ran(self) -> int:
        return self.tests - self.skipped

    def untolerated_skips(self, tolerated: Sequence[str]) -> tuple[str, ...]:
        """Skip reasons matching none of the tolerated markers.

        Fail-closed: an unrecognised reason counts as untolerated. A skip nobody has
        classified is exactly the case where the gate should decline to report a number.
        """
        return tuple(
            reason
            for reason in self.skip_reasons
            if not any(marker in reason for marker in tolerated)
        )


def _run_pytest(
    repo_root: Path, targets: Sequence[str], *, extra: Sequence[str] = ()
) -> _PytestRun:
    """Run part of the suite in a subprocess and read the counts back.

    A subprocess rather than `pytest.main`, deliberately. The suite has a root
    `conftest.py` that loads `.env` and probes two services during collection, and
    running that inside the gate's own interpreter would let a collection side effect
    change the environment the remaining checks measure in. The known trap about Alembic
    reconfiguring logging in-process is the same class of problem.

    Counts come from the JUnit report rather than from parsing `-q` output, because the
    summary line's wording is not a stable interface and a mis-parse would silently read
    as zero failures.
    """
    with tempfile.TemporaryDirectory(prefix="jutsu-gate-") as work:
        report = Path(work) / "junit.xml"
        argv = [
            sys.executable,
            "-m",
            "pytest",
            *targets,
            *extra,
            "-q",
            "--tb=no",
            "-p",
            "no:cacheprovider",
            f"--junit-xml={report}",
        ]
        # argv is built from constants and repo-relative test paths chosen in this
        # module; nothing here comes from a request or a corpus.
        completed = subprocess.run(  # noqa: S603
            argv,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )

        if not report.is_file():
            # Collection error, a missing plugin, or an interpreter that never started.
            return _PytestRun(completed.returncode, 0, 0, 0, 0)

        # Written moments ago by the pytest process started above, into a temporary
        # directory created above. Not attacker-reachable input.
        root = ElementTree.parse(report).getroot()  # noqa: S314
        suites = root.iter("testsuite")
        tests = failures = errors = skipped = 0
        for suite in suites:
            tests += int(suite.get("tests", "0"))
            failures += int(suite.get("failures", "0"))
            errors += int(suite.get("errors", "0"))
            skipped += int(suite.get("skipped", "0"))

        # Reasons, deduplicated and sorted, taken from the same report rather than from
        # a second `-rs` run. Three tests skipping for one reason is one fact.
        reasons = sorted(
            {
                (element.get("message") or "").strip()
                for case in root.iter("testcase")
                for element in case.iter("skipped")
            }
        )

    return _PytestRun(completed.returncode, tests, failures, errors, skipped, tuple(reasons))


def _suite_result(
    name: str,
    clause: str,
    run: _PytestRun,
    *,
    max_failures: int,
    max_skips: int,
    what: str,
) -> CheckResult:
    """Turn a pytest run into a verdict, with skips treated as unmeasured."""
    if run.tests == 0:
        return CheckResult.unmeasured(name, clause, f"{what} collected no tests")
    if run.failures + run.errors > max_failures:
        return CheckResult.failure(
            name,
            clause,
            f"{run.failures} failed and {run.errors} errored out of {run.tests}",
            observed=run.failures + run.errors,
            threshold=max_failures,
        )
    if run.skipped > max_skips:
        return CheckResult.unmeasured(
            name,
            clause,
            f"{run.skipped} of {run.tests} tests skipped, so the property is unproven "
            f"— start the services and re-run",
        )
    return CheckResult.ok(
        name,
        clause,
        f"{run.ran} tests passed, none skipped",
        observed=run.failures + run.errors,
        threshold=max_failures,
    )


# ------------------------------------------------------------------ corpus-scale checks

_CLAUSE_DOCUMENTS = "≥45k documents ingested"


async def check_documents_ingested(ctx: GateContext) -> CheckResult:
    name = "documents_ingested"
    reason = _needs_org(ctx)
    if reason:
        return CheckResult.unmeasured(name, _CLAUSE_DOCUMENTS, reason)
    unavailable = await _database_unavailable()
    if unavailable:
        return CheckResult.unmeasured(name, _CLAUSE_DOCUMENTS, unavailable)

    assert ctx.org_id is not None  # noqa: S101 - narrowed by _needs_org above
    async with org_session(ctx.org_id) as session:
        total = (
            await session.execute(
                text("SELECT count(*) FROM documents WHERE superseded_by IS NULL")
            )
        ).scalar_one()

    threshold = PHASE_1.min_documents
    detail = f"{total:,} current-version documents visible to the application role"
    if total >= threshold:
        return CheckResult.ok(name, _CLAUSE_DOCUMENTS, detail, observed=total, threshold=threshold)
    return CheckResult.failure(name, _CLAUSE_DOCUMENTS, detail, observed=total, threshold=threshold)


_CLAUSE_IDEMPOTENT = "second `make seed` adds zero rows"

#: The three tables a re-seed would touch if it were not idempotent.
_COUNTED_TABLES = ("documents", "chunks", "document_acl")


async def _row_counts(session: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _COUNTED_TABLES:
        # Table names come from the module constant above, never from input.
        counts[table] = (
            await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
        ).scalar_one()
    return counts


async def check_seed_idempotent(ctx: GateContext) -> CheckResult:
    name = "seed_idempotent"
    reason = _needs_org(ctx)
    if reason:
        return CheckResult.unmeasured(name, _CLAUSE_IDEMPOTENT, reason)
    if not ctx.allow_writes:
        return CheckResult.unmeasured(
            name,
            _CLAUSE_IDEMPOTENT,
            "this check re-runs ingestion and therefore writes; pass --allow-writes",
        )
    if ctx.reseed is None:
        return CheckResult.unmeasured(
            name, _CLAUSE_IDEMPOTENT, "no ingestion entry point was wired into the gate"
        )
    unavailable = await _database_unavailable()
    if unavailable:
        return CheckResult.unmeasured(name, _CLAUSE_IDEMPOTENT, unavailable)

    assert ctx.org_id is not None  # noqa: S101 - narrowed by _needs_org above
    async with org_session(ctx.org_id) as session:
        roots = [
            row.root
            for row in (
                await session.execute(
                    text(
                        "SELECT config_json ->> 'root' AS root FROM sources "
                        "WHERE system = 'local' AND config_json ? 'root'"
                    )
                )
            ).all()
        ]
        before = await _row_counts(session)

    if not roots:
        return CheckResult.unmeasured(
            name, _CLAUSE_IDEMPOTENT, "no local corpus has been seeded in this organisation"
        )

    for root in roots:
        await ctx.reseed(ctx.org_id, root)

    async with org_session(ctx.org_id) as session:
        after = await _row_counts(session)

    added = sum(after[table] - before[table] for table in _COUNTED_TABLES)
    per_table = ", ".join(f"{t} {after[t] - before[t]:+d}" for t in _COUNTED_TABLES)
    detail = f"re-ran {len(roots)} source(s): {per_table}"

    if added <= PHASE_1.max_new_rows_on_reseed:
        return CheckResult.ok(
            name,
            _CLAUSE_IDEMPOTENT,
            detail,
            observed=added,
            threshold=PHASE_1.max_new_rows_on_reseed,
        )
    return CheckResult.failure(
        name, _CLAUSE_IDEMPOTENT, detail, observed=added, threshold=PHASE_1.max_new_rows_on_reseed
    )


_CLAUSE_OFFSETS = "every chunk offset resolves to matching original text"


def remask_slice(original: str, spans: Sequence[MaskedSpan], start: int, end: int) -> str:
    """The stored original range, masked the way the pipeline masks it.

    This is the check's whole point, so it is worth being explicit about what it does
    *not* do: it does not call the chunker. Replaying `chunk_document` would prove the
    pipeline is deterministic, which is a different and much weaker claim — a chunker
    with an off-by-one reproduces the same off-by-one perfectly.

    Instead it takes the two numbers actually stored on the row, slices the original
    body with them, substitutes the mask tokens whose spans fall inside that slice, and
    the result must equal the stored chunk text. Nothing but `mask` and string slicing is
    involved, so the arithmetic under test is not also the arithmetic doing the testing.
    """
    pieces: list[str] = []
    cursor = start
    for span in spans:
        if span.orig_end <= start or span.orig_start >= end:
            continue
        pieces.append(original[cursor : span.orig_start])
        pieces.append(span.token)
        cursor = span.orig_end
    pieces.append(original[cursor:end])
    return "".join(pieces)


async def _sample_document_ids(session: AsyncSession, ctx: GateContext) -> list[uuid.UUID]:
    """Deterministically chosen current-version documents.

    Ordered by a hash of the id and the run seed rather than by `random()`, so two runs
    over the same corpus examine the same documents and their results can be compared.
    """
    sql = "SELECT id FROM documents WHERE superseded_by IS NULL ORDER BY md5(id::text || :seed)"
    params: dict[str, object] = {"seed": str(ctx.seed)}
    if ctx.sample is not None:
        sql += " LIMIT :limit"
        params["limit"] = ctx.sample
    rows = (await session.execute(text(sql), params)).all()
    return [uuid.UUID(str(row.id)) for row in rows]


async def check_offsets_resolve(ctx: GateContext) -> CheckResult:
    name = "offsets_resolve"
    reason = _needs_org(ctx)
    if reason:
        return CheckResult.unmeasured(name, _CLAUSE_OFFSETS, reason)
    unavailable = await _database_unavailable()
    if unavailable:
        return CheckResult.unmeasured(name, _CLAUSE_OFFSETS, unavailable)

    assert ctx.org_id is not None  # noqa: S101 - narrowed by _needs_org above
    mismatches = 0
    checked = 0
    documents = 0

    async with org_session(ctx.org_id) as session:
        ids = await _sample_document_ids(session, ctx)
        if not ids:
            return CheckResult.unmeasured(
                name, _CLAUSE_OFFSETS, "no documents in this organisation"
            )

        for offset in range(0, len(ids), _OFFSET_BATCH):
            batch = [str(i) for i in ids[offset : offset + _OFFSET_BATCH]]
            bodies = {
                str(row.id): row.body_original
                for row in (
                    await session.execute(
                        text("SELECT id, body_original FROM documents WHERE id = ANY(:ids)"),
                        {"ids": batch},
                    )
                ).all()
            }
            rows = (
                await session.execute(
                    text(
                        "SELECT document_id, ordinal, text, char_start, char_end FROM chunks "
                        "WHERE document_id = ANY(:ids) ORDER BY document_id, ordinal"
                    ),
                    {"ids": batch},
                )
            ).all()

            spans_by_document: dict[str, list[MaskedSpan]] = {}
            for row in rows:
                document_id = str(row.document_id)
                original = bodies.get(document_id)
                if original is None:
                    continue
                if document_id not in spans_by_document:
                    # Namespaced by document id, exactly as `persist_document` does it.
                    spans_by_document[document_id] = mask(original, namespace=document_id).spans
                    documents += 1

                checked += 1
                expected = remask_slice(
                    original, spans_by_document[document_id], row.char_start, row.char_end
                )
                if expected != row.text:
                    mismatches += 1

    if checked == 0:
        return CheckResult.unmeasured(name, _CLAUSE_OFFSETS, "the sampled documents have no chunks")

    scope = "every document" if ctx.sample is None else f"a sample of {len(ids)} documents"
    detail = f"{checked:,} chunk offsets replayed across {documents:,} documents ({scope})"
    if mismatches <= PHASE_1.max_offset_mismatches:
        return CheckResult.ok(
            name,
            _CLAUSE_OFFSETS,
            detail,
            observed=mismatches,
            threshold=PHASE_1.max_offset_mismatches,
        )
    return CheckResult.failure(
        name,
        _CLAUSE_OFFSETS,
        f"{mismatches:,} of {detail}",
        observed=mismatches,
        threshold=PHASE_1.max_offset_mismatches,
    )


_CLAUSE_LOGS = "zero raw PII in captured logs"


async def check_no_raw_pii_in_logs(ctx: GateContext) -> CheckResult:
    """Scan a captured run log with the pipeline's own detectors.

    **It reports types and counts, never a value.** A check that proved there is no PII
    in the logs by printing the PII it found into a committed report would have created
    the leak it was hired to find.
    """
    name = "no_raw_pii_in_logs"
    if ctx.log_path is None:
        return CheckResult.unmeasured(
            name, _CLAUSE_LOGS, "no --log was given, so no captured log was scanned"
        )
    if not ctx.log_path.is_file():
        return CheckResult.unmeasured(name, _CLAUSE_LOGS, "the --log path is not a file")

    by_type: dict[str, int] = {}
    lines = 0
    try:
        with ctx.log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                lines += 1
                for span in mask(line).spans:
                    by_type[span.pii_type.value] = by_type.get(span.pii_type.value, 0) + 1
    except OSError:
        return CheckResult.unmeasured(name, _CLAUSE_LOGS, "the --log file could not be read")

    total = sum(by_type.values())
    if total <= PHASE_1.max_pii_detections_in_logs:
        return CheckResult.ok(
            name,
            _CLAUSE_LOGS,
            f"{lines:,} log lines scanned, no detector matched",
            observed=total,
            threshold=PHASE_1.max_pii_detections_in_logs,
        )
    breakdown = ", ".join(f"{kind} x{count}" for kind, count in sorted(by_type.items()))
    return CheckResult.failure(
        name,
        _CLAUSE_LOGS,
        f"{total} detections across {lines:,} lines ({breakdown}) — values deliberately not shown",
        observed=total,
        threshold=PHASE_1.max_pii_detections_in_logs,
    )


_CLAUSE_EMBEDDED = "100% chunks embedded at dim 768"


async def check_chunks_embedded(ctx: GateContext) -> CheckResult:
    name = "chunks_embedded"
    reason = _needs_org(ctx)
    if reason:
        return CheckResult.unmeasured(name, _CLAUSE_EMBEDDED, reason)
    unavailable = await _database_unavailable()
    if unavailable:
        return CheckResult.unmeasured(name, _CLAUSE_EMBEDDED, unavailable)

    assert ctx.org_id is not None  # noqa: S101 - narrowed by _needs_org above
    async with org_session(ctx.org_id) as session:
        # Current versions only. A superseded version's chunks were embedded when that
        # version was current, and demanding vectors for history would fail a corpus
        # that is behaving exactly as S8 intends.
        row = (
            await session.execute(
                text(
                    "SELECT count(*) AS total, "
                    "count(*) FILTER (WHERE c.embedding IS NULL) AS missing "
                    "FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE d.superseded_by IS NULL"
                )
            )
        ).one()
        dims = sorted(
            int(value)
            for value in (
                await session.execute(
                    text(
                        "SELECT DISTINCT vector_dims(c.embedding) AS dim FROM chunks c "
                        "JOIN documents d ON d.id = c.document_id "
                        "WHERE d.superseded_by IS NULL AND c.embedding IS NOT NULL"
                    )
                )
            )
            .scalars()
            .all()
        )

    if row.total == 0:
        return CheckResult.unmeasured(
            name, _CLAUSE_EMBEDDED, "no current-version chunks in this organisation"
        )

    if row.missing > PHASE_1.max_unembedded_chunks:
        return CheckResult.failure(
            name,
            _CLAUSE_EMBEDDED,
            f"{row.missing:,} of {row.total:,} current-version chunks have no vector",
            observed=row.missing,
            threshold=PHASE_1.max_unembedded_chunks,
        )
    if dims != [PHASE_1.embedding_dim]:
        return CheckResult.failure(
            name,
            _CLAUSE_EMBEDDED,
            f"stored vector widths are {dims}, and §21 requires exactly [{PHASE_1.embedding_dim}]",
            observed=str(dims),
            threshold=PHASE_1.embedding_dim,
        )
    return CheckResult.ok(
        name,
        _CLAUSE_EMBEDDED,
        f"all {row.total:,} current-version chunks embedded at dim {PHASE_1.embedding_dim}",
        observed=row.missing,
        threshold=PHASE_1.max_unembedded_chunks,
    )


# ------------------------------------------------------------------- suite-backed checks

_CLAUSE_ACL = "all 7 ACL tests pass"
_CLAUSE_ISOLATION = "org isolation proven"
_CLAUSE_REVERSIBLE = "migrations reversible"

#: The suite the "7 ACL tests" clause grew into — S7 delivered 39 adversarial tests
#: against it, including the `EXPLAIN` assertion that the filter is in the SQL.
ACL_SUITE = "packages/retrieval/tests/test_search_acl.py"

#: Tenant isolation, both halves: the RLS policies themselves and the identity and
#: membership resolution that feeds them.
ISOLATION_SUITE = (
    "packages/db/tests/test_rls.py",
    "packages/db/tests/test_tenancy_and_identity.py",
)

#: The existing fingerprint round trip. Reused rather than reimplemented: it already
#: snapshots columns, constraints, indexes, policies, RLS flags and enum labels, and a
#: second implementation would be a second opinion about what "reversible" means.
REVERSIBILITY_SUITE = "packages/db/tests/test_migration.py"

#: Selected with `-k` rather than by node id. A node id has to name the class, so
#: `TestReversibility` becomes part of this module's contract with a file it does not own
#: — and moving the test into or out of a class would silently turn the clause into
#: "collected no tests", which is unmeasured rather than red. Found by running it.
REVERSIBILITY_TEST = "test_downgrade_then_upgrade_restores_identical_schema"


async def check_acl_suite(ctx: GateContext) -> CheckResult:
    run = await asyncio.to_thread(_run_pytest, ctx.repo_root, [ACL_SUITE])
    return _suite_result(
        "acl_suite",
        _CLAUSE_ACL,
        run,
        max_failures=PHASE_1.max_acl_failures,
        max_skips=PHASE_1.max_acl_skips,
        what="the ACL suite",
    )


async def check_org_isolation(ctx: GateContext) -> CheckResult:
    run = await asyncio.to_thread(_run_pytest, ctx.repo_root, list(ISOLATION_SUITE))
    return _suite_result(
        "org_isolation",
        _CLAUSE_ISOLATION,
        run,
        max_failures=PHASE_1.max_isolation_failures,
        max_skips=PHASE_1.max_isolation_skips,
        what="the isolation suite",
    )


async def check_migrations_reversible(ctx: GateContext) -> CheckResult:
    """Round-trip the schema, against the **test** database.

    The suite this delegates to points at `JUTSU_TEST_MIGRATION_URL`, which is the only
    safe place to run it: downgrading the seeded database to base to prove a property
    would destroy the corpus every other check measures. The related trap is real and
    stays reported rather than worked around — `migrate-pg-down` refuses once a document
    has been superseded, because the pre-0010 constraint cannot represent version
    history, and on such a database this clause is legitimately unmeasured.
    """
    run = await asyncio.to_thread(
        _run_pytest, ctx.repo_root, [REVERSIBILITY_SUITE], extra=["-k", REVERSIBILITY_TEST]
    )
    return _suite_result(
        "migrations_reversible",
        _CLAUSE_REVERSIBLE,
        run,
        max_failures=0,
        max_skips=0,
        what="the migration round trip",
    )


# ------------------------------------------------------------------ preflight + coverage

_CLAUSE_PREFLIGHT = "preflight green"
_CLAUSE_COVERAGE = "≥70% coverage on core/graph/retrieval"


#: What `make` is called here. This repo is developed on Windows against MinGW, where the
#: binary is `mingw32-make` and there is no `make` at all — so a gate that only looked for
#: one name would report the preflight clause unmeasured on the machine it was written on.
#: Found by running it.
_MAKE_NAMES = ("make", "mingw32-make", "gmake")


def _find_make() -> str | None:
    for name in _MAKE_NAMES:
        found = shutil.which(name)
        if found is not None:
            return found
    return None


def _run_make(make: str, repo_root: Path) -> subprocess.CompletedProcess[str]:
    """`make preflight`, with the binary already resolved by `shutil.which`."""
    return subprocess.run(  # noqa: S603 - resolved binary, one literal target, no shell
        [make, "preflight"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
        check=False,
    )


async def check_preflight(ctx: GateContext) -> CheckResult:
    name = "preflight"
    make = _find_make()
    if make is None:
        return CheckResult.unmeasured(
            name,
            _CLAUSE_PREFLIGHT,
            f"none of {', '.join(_MAKE_NAMES)} is on PATH, so preflight could not be run",
        )

    completed = await asyncio.to_thread(_run_make, make, ctx.repo_root)
    if completed.returncode == 0:
        return CheckResult.ok(
            name, _CLAUSE_PREFLIGHT, "`make preflight` exited 0", observed=0, threshold=0
        )
    return CheckResult.failure(
        name,
        _CLAUSE_PREFLIGHT,
        f"`make preflight` exited {completed.returncode}",
        observed=completed.returncode,
        threshold=0,
    )


#: Where each measured package's sources live, so coverage can be attributed per package
#: rather than as one blended number that a large well-tested package could carry.
_COVERAGE_ROOTS = {
    "jutsu_core": "packages/core/src/jutsu_core",
    "jutsu_graph": "packages/graph/src/jutsu_graph",
    "jutsu_retrieval": "packages/retrieval/src/jutsu_retrieval",
}


def _package_of(path: str) -> str | None:
    normalised = path.replace("\\", "/")
    for package, root in _COVERAGE_ROOTS.items():
        if root in normalised:
            return package
    return None


async def check_coverage_core(ctx: GateContext) -> CheckResult:
    """Per-package coverage, not a blended total.

    §21 names three packages. Averaging them lets a well-covered `jutsu_core` carry a
    thin `jutsu_graph` over the line, which is the opposite of what the clause is for,
    so the reported figure is the *lowest* of the three.
    """
    name = "coverage_core"
    with tempfile.TemporaryDirectory(prefix="jutsu-gate-cov-") as work:
        report = Path(work) / "coverage.json"
        extra = [
            *(f"--cov={package}" for package in PHASE_1.coverage_packages),
            f"--cov-report=json:{report}",
        ]
        run = await asyncio.to_thread(_run_pytest, ctx.repo_root, ["packages", "apps"], extra=extra)

        if not report.is_file():
            return CheckResult.unmeasured(
                name,
                _CLAUSE_COVERAGE,
                "no coverage report was produced — is pytest-cov installed?",
            )
        payload = json.loads(report.read_text(encoding="utf-8"))

    if run.failures + run.errors > 0:
        return CheckResult.unmeasured(
            name,
            _CLAUSE_COVERAGE,
            f"{run.failures + run.errors} tests failed during the coverage run, so the "
            f"figure describes a suite that did not pass",
        )

    # A skipped test covers nothing, so coverage taken with the containers down is a
    # measurement of a different suite. Found by running it: with Postgres and Neo4j
    # stopped, `jutsu_graph` reported 59.3% and the clause went red — a confident number
    # about a run where 394 tests never executed. Coverage is the one clause where a
    # skip does not merely leave a gap, it moves the figure.
    #
    # But refusing *every* skip made the clause permanently unmeasurable, because some
    # skips are structural: opt-in live-provider tests, a missing Windows privilege, and
    # two harness tests that skip precisely because the database is up. Those are
    # classified in `Phase1Thresholds.tolerated_skip_markers`; anything else still makes
    # the figure unreportable, and an unrecognised reason counts as anything else.
    untolerated = run.untolerated_skips(PHASE_1.tolerated_skip_markers)
    if untolerated:
        return CheckResult.unmeasured(
            name,
            _CLAUSE_COVERAGE,
            f"{run.skipped} of {run.tests} tests skipped, {len(untolerated)} of them for "
            f"reasons that change the figure — first: {untolerated[0][:90]}",
        )

    statements: dict[str, int] = {p: 0 for p in PHASE_1.coverage_packages}
    covered: dict[str, int] = {p: 0 for p in PHASE_1.coverage_packages}
    for path, entry in payload.get("files", {}).items():
        package = _package_of(path)
        if package is None:
            continue
        summary = entry.get("summary", {})
        statements[package] += int(summary.get("num_statements", 0))
        covered[package] += int(summary.get("covered_lines", 0))

    percentages: dict[str, float] = {}
    for package in PHASE_1.coverage_packages:
        if statements[package] == 0:
            return CheckResult.unmeasured(
                name, _CLAUSE_COVERAGE, f"{package} contributed no measured statements"
            )
        percentages[package] = round(100.0 * covered[package] / statements[package], 2)

    lowest = min(percentages, key=lambda p: percentages[p])
    detail = " · ".join(f"{p} {percentages[p]:.1f}%" for p in PHASE_1.coverage_packages)
    if run.skipped:
        # Named on the line rather than left implicit: the figure was taken over a suite
        # with known-structural skips, and the reader should be able to see that.
        detail += f" (over {run.ran} of {run.tests} tests; {run.skipped} tolerated skips)"

    if percentages[lowest] >= PHASE_1.min_coverage_percent:
        return CheckResult.ok(
            name,
            _CLAUSE_COVERAGE,
            detail,
            observed=percentages[lowest],
            threshold=PHASE_1.min_coverage_percent,
        )
    return CheckResult.failure(
        name,
        _CLAUSE_COVERAGE,
        f"{detail} — {lowest} is below the floor",
        observed=percentages[lowest],
        threshold=PHASE_1.min_coverage_percent,
    )


# ------------------------------------------------------------------------ cost recorded

_CLAUSE_COST = "seed-run token cost recorded"


async def check_token_cost_recorded(ctx: GateContext) -> CheckResult:
    name = "token_cost_recorded"
    reason = _needs_org(ctx)
    if reason:
        return CheckResult.unmeasured(name, _CLAUSE_COST, reason)

    assert ctx.org_id is not None  # noqa: S101 - narrowed by _needs_org above
    receipt = latest_receipt(ctx.repo_root, ctx.org_id)
    if receipt is None:
        return CheckResult.unmeasured(
            name, _CLAUSE_COST, "no seed receipt for this organisation under evals/runs/"
        )

    if not receipt.embedded:
        # Zero is a recorded cost, not a missing one — that run genuinely spent nothing.
        return CheckResult.ok(
            name,
            _CLAUSE_COST,
            f"receipt for {receipt.documents:,} documents in {receipt.elapsed_seconds:.1f}s, "
            f"run without --embed so 0 tokens were spent",
            observed=receipt.tokens,
            threshold=0,
        )
    return CheckResult.ok(
        name,
        _CLAUSE_COST,
        f"{receipt.tokens:,} tokens over {receipt.requests:,} requests for "
        f"{receipt.documents:,} documents and {receipt.chunks:,} chunks "
        f"({receipt.model} at dim {receipt.dimension}) in {receipt.elapsed_seconds:.1f}s",
        observed=receipt.tokens,
        threshold=0,
    )


#: The eleven checks, in the order §21 states the clauses.
PHASE_1_CHECKS: tuple[Check, ...] = (
    Check("documents_ingested", _CLAUSE_DOCUMENTS, check_documents_ingested),
    Check("seed_idempotent", _CLAUSE_IDEMPOTENT, check_seed_idempotent),
    Check("offsets_resolve", _CLAUSE_OFFSETS, check_offsets_resolve),
    Check("no_raw_pii_in_logs", _CLAUSE_LOGS, check_no_raw_pii_in_logs),
    Check("chunks_embedded", _CLAUSE_EMBEDDED, check_chunks_embedded),
    Check("acl_suite", _CLAUSE_ACL, check_acl_suite),
    Check("org_isolation", _CLAUSE_ISOLATION, check_org_isolation),
    Check("migrations_reversible", _CLAUSE_REVERSIBLE, check_migrations_reversible),
    Check("preflight", _CLAUSE_PREFLIGHT, check_preflight),
    Check("coverage_core", _CLAUSE_COVERAGE, check_coverage_core),
    Check("token_cost_recorded", _CLAUSE_COST, check_token_cost_recorded),
)
