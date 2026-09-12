"""Reciprocal rank fusion — combining two rankings without comparing their scores (§12).

The problem this solves is small and easy to get wrong. Vector search ranks by cosine
similarity; graph retrieval ranks by an extractor's confidence. Both are numbers in
`[0, 1]` and they mean entirely different things, so any scheme that adds, averages or
thresholds them is comparing a distance with a belief. A 0.8 similarity and a 0.8
confidence have nothing to say to each other.

RRF sidesteps that by throwing the scores away and keeping only the positions:

    score(d) = Σ over rankings  1 / (k + rank(d))

An item near the top of either list scores well; an item present in both scores better
than either alone; and nothing has to know what the underlying numbers meant. `k = 60` is
the constant §12 names, and it is not arbitrary — it flattens the difference between the
first few positions, so a single confident list cannot dominate a fusion the way `k = 1`
would.

**Deterministic by construction.** Ties are broken by first appearance, scanning the
rankings in the order they were given, so the same inputs always produce the same output
order. A hybrid result set that reshuffles under a reader between identical requests is
indistinguishable from a bug in the retrieval, and it makes every test that asserts an
order flaky rather than wrong.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from typing import Final

__all__ = ["RRF_K", "reciprocal_rank_fusion"]

#: §12: "fusion: reciprocal rank fusion (k=60)". The spec's constant, named once.
RRF_K: Final = 60


def reciprocal_rank_fusion[T: Hashable](
    rankings: Sequence[Sequence[T]], *, k: int = RRF_K
) -> list[tuple[T, float]]:
    """Fuse ranked lists into one, best first, with each item's fused score.

    `rankings` is a sequence of ranked lists — most relevant first within each. An item
    may appear in any number of them; duplicates *within* one list are ignored after the
    first, because a list that ranks the same item twice is expressing one opinion, and
    counting it twice would let a caller inflate an item by repeating it.

    Returns `(item, score)` pairs rather than bare items so a caller can report why an
    order came out the way it did — a fused score with no provenance is a number nobody
    can check.

    `k` must be positive: at `k = 0` a rank-0 item divides by zero, and the whole point of
    the constant is to damp the top of each list rather than to sharpen it.
    """
    if k <= 0:
        raise ValueError(f"k must be positive; {k} would make the first rank infinite.")

    scores: dict[T, float] = {}
    first_seen: dict[T, int] = {}
    order = 0

    for ranking in rankings:
        seen_here: set[T] = set()
        for rank, item in enumerate(ranking, start=1):
            if item in seen_here:
                continue
            seen_here.add(item)
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            if item not in first_seen:
                first_seen[item] = order
                order += 1

    return sorted(scores.items(), key=lambda pair: (-pair[1], first_seen[pair[0]]))
