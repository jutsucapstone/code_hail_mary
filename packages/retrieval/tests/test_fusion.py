"""Reciprocal rank fusion (§12).

Pure arithmetic over positions, so these are pure tests: no database, no graph, no
provider. What they pin is the behaviour a hybrid retrieval depends on and which is easy
to break while the numbers still look plausible — that presence in both lists beats
presence in one, that the constant is the spec's, and that the order is the same every
time it is computed.
"""

from __future__ import annotations

import pytest
from jutsu_retrieval.fusion import RRF_K, reciprocal_rank_fusion


class TestTheConstant:
    def test_k_is_the_spec_value(self) -> None:
        # §12: "fusion: reciprocal rank fusion (k=60)". A different k is a different
        # ranking, so the number is part of the contract rather than a tuning knob
        # somebody may quietly change.
        assert RRF_K == 60

    def test_a_non_positive_k_is_refused(self) -> None:
        # At k=0 the first rank divides by zero. The constant damps the top of each list;
        # a k that sharpens it is a misunderstanding worth failing on.
        with pytest.raises(ValueError, match="positive"):
            reciprocal_rank_fusion([["a"]], k=0)


class TestFusing:
    def test_a_single_ranking_keeps_its_order(self) -> None:
        fused = reciprocal_rank_fusion([["a", "b", "c"]])

        assert [item for item, _ in fused] == ["a", "b", "c"]

    def test_an_item_in_both_rankings_beats_an_item_in_one(self) -> None:
        # The whole point. `b` is second in both lists and never first in either; `a` is
        # first in one and absent from the other. Agreement wins.
        fused = reciprocal_rank_fusion([["a", "b"], ["c", "b"]])

        assert next(item for item, _ in fused) == "b"

    def test_scores_are_the_sum_of_reciprocal_ranks(self) -> None:
        fused = dict(reciprocal_rank_fusion([["a", "b"], ["b"]], k=60))

        assert fused["a"] == pytest.approx(1 / 61)
        assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)

    def test_an_empty_ranking_contributes_nothing(self) -> None:
        # A graph half that found nothing must leave the vector half exactly as it was —
        # this is the fallback contract expressed in arithmetic.
        vector = ["a", "b", "c"]

        assert [item for item, _ in reciprocal_rank_fusion([vector, []])] == vector

    def test_everything_empty_is_an_empty_result(self) -> None:
        assert reciprocal_rank_fusion([[], []]) == []

    def test_a_repeat_within_one_ranking_counts_once(self) -> None:
        # A list that ranks the same item twice is expressing one opinion. Counting it
        # twice would let a caller inflate an item by repeating it, which is the shape of
        # a ranking attack rather than a ranking.
        once = dict(reciprocal_rank_fusion([["a", "b"]]))
        twice = dict(reciprocal_rank_fusion([["a", "a", "b"]]))

        assert once["a"] == twice["a"]


class TestDeterminism:
    def test_ties_break_by_first_appearance(self) -> None:
        # `a` and `b` tie exactly: each is first in one list. The order is then decided by
        # which was seen first, scanning the rankings in the order given — not by a set's
        # iteration order, which changes between processes.
        fused = reciprocal_rank_fusion([["a"], ["b"]])

        assert [item for item, _ in fused] == ["a", "b"]

    def test_the_same_input_always_fuses_the_same_way(self) -> None:
        # A hybrid result set that reshuffles between identical requests is
        # indistinguishable from a retrieval bug, and makes every ordering test flaky
        # rather than wrong.
        rankings = [["a", "b", "c", "d"], ["d", "c", "x", "a"]]

        first = reciprocal_rank_fusion(rankings)
        for _ in range(5):
            assert reciprocal_rank_fusion(rankings) == first
