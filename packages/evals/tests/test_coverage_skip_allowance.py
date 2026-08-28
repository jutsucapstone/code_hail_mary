"""Which skips invalidate a coverage figure, and which are simply facts about the box.

Coverage is the one M1 clause where a skip *moves the number* rather than leaving a gap —
with the containers down `jutsu_graph` measured 59.3% and the clause went red about a run
where 394 tests never executed. So the first implementation refused any skip at all.

That was unsatisfiable. Five of the seven skips in a fully-provisioned checkout are
structural: the live-provider tests are opt-in because they spend money, the symlink
containment tests need a Windows privilege the dev box does not hold, and two harness
tests skip *because* the database is up. No amount of starting containers removes them,
so the clause could never be measured, which is its own kind of useless.

The allowance is therefore a classification of *reasons*, not a count — a count drifts
silently as tests are added — and it is fail-closed: a reason nobody has classified makes
the figure unreportable.
"""

from __future__ import annotations

from jutsu_evals.phase1 import _PytestRun
from jutsu_evals.thresholds import PHASE_1

#: The three reasons observed in this repository, verbatim enough to match.
LIVE = "live provider call; set JUTSU_LIVE_EMBEDDING_SMOKE=1 to run (spends a small amount)"
SYMLINK = "symlinks unavailable on this platform: [WinError 1314] A required privilege"
INVERSE = "the database is up, so these paths cannot be reached"

#: The reason that must never be tolerated — it is the one that moves the number.
CONTAINERS_DOWN = "nothing listening at JUTSU_TEST_DATABASE_URL — start Postgres with `make up`"


def _run(*reasons: str, skipped: int | None = None) -> _PytestRun:
    return _PytestRun(
        exit_code=0,
        tests=1153,
        failures=0,
        errors=0,
        skipped=len(reasons) if skipped is None else skipped,
        skip_reasons=reasons,
    )


class TestStructuralSkipsAreTolerated:
    def test_the_three_observed_reasons_all_pass(self) -> None:
        run = _run(LIVE, SYMLINK, INVERSE, skipped=7)
        assert run.untolerated_skips(PHASE_1.tolerated_skip_markers) == ()

    def test_a_fully_provisioned_checkout_is_measurable(self) -> None:
        """The point of the change: M1's coverage clause must be reachable."""
        run = _run(LIVE, SYMLINK, INVERSE, skipped=7)
        assert not run.untolerated_skips(PHASE_1.tolerated_skip_markers)
        assert run.ran == 1146

    def test_no_skips_at_all_is_obviously_fine(self) -> None:
        assert _run().untolerated_skips(PHASE_1.tolerated_skip_markers) == ()


class TestEnvironmentalSkipsAreNotTolerated:
    def test_containers_down_still_makes_the_figure_unreportable(self) -> None:
        """The regression this allowance must not become."""
        run = _run(CONTAINERS_DOWN)
        assert run.untolerated_skips(PHASE_1.tolerated_skip_markers) == (CONTAINERS_DOWN,)

    def test_one_environmental_skip_among_tolerated_ones_still_counts(self) -> None:
        run = _run(LIVE, SYMLINK, CONTAINERS_DOWN, INVERSE)
        assert run.untolerated_skips(PHASE_1.tolerated_skip_markers) == (CONTAINERS_DOWN,)

    def test_an_unclassified_reason_is_untolerated(self) -> None:
        """Fail-closed. A new skip has to be understood before it is allowed."""
        run = _run("some new reason nobody has classified yet")
        assert len(run.untolerated_skips(PHASE_1.tolerated_skip_markers)) == 1

    def test_an_empty_reason_is_untolerated(self) -> None:
        """A junit entry with no message tells us nothing, so it cannot be waved through."""
        assert _run("").untolerated_skips(PHASE_1.tolerated_skip_markers) == ("",)


class TestTheMarkersThemselves:
    def test_there_are_exactly_three_and_each_is_specific(self) -> None:
        """A marker short enough to match anything would tolerate everything."""
        assert len(PHASE_1.tolerated_skip_markers) == 3
        for marker in PHASE_1.tolerated_skip_markers:
            assert len(marker) > 20, marker

    def test_no_marker_matches_the_containers_down_reason(self) -> None:
        assert not any(m in CONTAINERS_DOWN for m in PHASE_1.tolerated_skip_markers)

    def test_the_markers_are_frozen_with_the_rest(self) -> None:
        import pytest

        with pytest.raises((AttributeError, TypeError)):
            PHASE_1.tolerated_skip_markers = ()  # type: ignore[misc]
