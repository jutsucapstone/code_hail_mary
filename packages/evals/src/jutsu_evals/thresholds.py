"""The M1 numbers, in one place, taken from §21 (CLAUDE.md rule 8).

Every figure here is quoted from the spec's Gate M1 paragraph, not from the code that
happens to implement it. That direction matters. `EMBEDDING_DIM` is configuration — an
operator can change it and the application will keep working — while §21 says the gate
must see 768. If those two ever disagree the gate has to report a failure, which it can
only do if it is holding the spec's number rather than reading the application's.

A threshold inlined at a call site is a goalpost that can move without a diff. There is
a test asserting each field against the spec text.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PHASE_1", "Phase1Thresholds"]


@dataclass(frozen=True, slots=True)
class Phase1Thresholds:
    """§21 · Gate M1, clause by clause."""

    #: "≥45k documents ingested"
    min_documents: int = 45_000
    #: "second `make seed` adds zero rows"
    max_new_rows_on_reseed: int = 0
    #: "every chunk offset resolves to matching original text"
    max_offset_mismatches: int = 0
    #: "zero raw PII in captured logs"
    max_pii_detections_in_logs: int = 0
    #: "100% chunks embedded at dim 768"
    max_unembedded_chunks: int = 0
    embedding_dim: int = 768
    #: "all 7 ACL tests pass" — the suite that grew out of that clause is
    #: `test_search_acl.py`. Zero failures, and zero skips: the reason `conftest.py`
    #: probes reachability at all is that this suite once reported green having run
    #: nothing, and a skipped ACL test is not a passing one.
    max_acl_failures: int = 0
    max_acl_skips: int = 0
    #: "org isolation proven" — same rule, same reason.
    max_isolation_failures: int = 0
    max_isolation_skips: int = 0
    #: "preflight green with ≥70% coverage on core/graph/retrieval"
    min_coverage_percent: float = 70.0
    coverage_packages: tuple[str, ...] = ("jutsu_core", "jutsu_graph", "jutsu_retrieval")

    #: Skip reasons that do **not** invalidate a coverage measurement.
    #:
    #: Coverage is the one clause where a skip moves the number rather than leaving a
    #: gap, so the first implementation refused any skip at all. That was too strict to
    #: be satisfiable: five of the seven skips in a fully-provisioned checkout are
    #: structural rather than environmental, and no amount of starting containers
    #: removes them. The clause became permanently unmeasurable, which is a different
    #: way of being useless.
    #:
    #: So the allowance is a *classification*, not a count. A count would drift silently
    #: as tests are added; these three substrings name the three reasons that are
    #: legitimately unfixable here:
    #:
    #:   * the live-provider smoke tests are opt-in because they spend money;
    #:   * the symlink containment tests need a Windows privilege the dev box lacks;
    #:   * two harness tests skip *because* the database is up — an inverse guard, where
    #:     skipping is the correct behaviour and running would be the bug.
    #:
    #: **Fail-closed:** anything not matching one of these is environmental as far as
    #: this gate is concerned, and makes the clause unmeasured. A new skip has to be
    #: understood and added here deliberately, which is the point.
    tolerated_skip_markers: tuple[str, ...] = (
        "set JUTSU_LIVE_EMBEDDING_SMOKE",
        "symlinks unavailable on this platform",
        "the database is up, so these paths cannot be reached",
    )


#: The instance every phase-1 check reads. Constructed once so a check cannot quietly
#: build one with a friendlier number.
PHASE_1 = Phase1Thresholds()
