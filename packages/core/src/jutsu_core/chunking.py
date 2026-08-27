"""Chunking with original-document offsets (spec §9.2).

A chunk carries two things that live in different coordinate systems, and keeping them
straight is the whole job:

    text         the MASKED text. This is what gets embedded and what the model reads.
    char_start   offsets into the ORIGINAL document body. This is what the citation UI
    char_end     highlights.

`len(chunk.text)` therefore does **not** equal `char_end - char_start`, and a test that
assumed it did would be enforcing the exact bug this module exists to avoid. Every
boundary is produced in masked coordinates and converted through
`MaskResult.original_range` before it is stored — that function is the only sanctioned
bridge, and an off-by-one in it is a visible product bug rather than a rounding error.

**The split hierarchy** is §9.2's: headings, then paragraph breaks, then sentence breaks,
then a hard token limit. Implemented as a boundary *strength* on each split point rather
than as four separate passes, so packing can prefer to close a chunk at the strongest
boundary it can reach while still filling its budget.

**Two rules are absolute.**

  * A chunk boundary never falls strictly inside a `MaskedSpan`. Splitting a token would
    leave `[EMAI` in one chunk and `L_A7]` in the next, which is both unembeddable and a
    silent corruption of the offset map.
  * A sentence is never split unless that single sentence exceeds the hard limit on its
    own. §9.2 also asks that a chunk never split "across a decision statement"; deciding
    what *is* a decision requires the extraction layer of §10, which does not exist yet,
    so this slice implements the reachable half of that requirement — a decision
    statement written as a sentence survives intact. True decision identification belongs
    to knowledge extraction and is explicitly out of scope here.

**Token counting is injected.** `chunks.token_count` and `target_tokens` both mean tokens
as the *embedding model* counts them, and that tokeniser is not available in this package:
Gemini's is served over the network, and adding a local SentencePiece model to `core`
would contradict ADR 0001's rule that this package stays import-cheap and dependency-light.
So the counter is a parameter, `estimate_tokens` is a documented **estimate** used as the
default for local operation and unit testing, and S6 supplies the real one before anything
is persisted. See ADR 0006. Nothing here should be read as an exact model token count.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Final, Protocol

from jutsu_core.models import Chunk, MaskResult

__all__ = [
    "DEFAULT_MIN_TOKENS",
    "DEFAULT_OVERLAP_RATIO",
    "DEFAULT_TARGET_TOKENS",
    "TokenCounter",
    "chunk_document",
    "estimate_tokens",
]

#: §9.2's defaults, named so a caller can reference them rather than repeat them.
DEFAULT_TARGET_TOKENS: Final = 768
DEFAULT_OVERLAP_RATIO: Final = 0.15
DEFAULT_MIN_TOKENS: Final = 128


class TokenCounter(Protocol):
    """How many tokens a piece of text costs.

    A callable rather than a class, so S6 can pass a bound method on an embedding client,
    a closure over a loaded tokeniser, or a stub — without this module knowing which.

    It must be **pure and deterministic**: chunk boundaries are decided by its answers, so
    a counter that varied would make ingestion non-idempotent (§4.14).

    The argument is **positional-only**, and that slash is load-bearing. Without it the
    protocol demands a parameter literally named `text`, so a perfectly good
    `Callable[[str], int]` — a `lambda`, a `partial`, a tokeniser wrapper that happens to
    call its argument `content` — fails to satisfy it. Nothing here ever passes it by
    keyword, so requiring the name buys nothing and rejects most of the callers S6 is
    likely to have.
    """

    def __call__(self, text: str, /) -> int: ...


def estimate_tokens(text: str) -> int:
    """An ESTIMATE of token cost. Never an exact model token count.

    **It is not reliably conservative, and an earlier version of this docstring claimed it
    was.** Measured against the real tokenizer in S6: ascii prose 1.43x, Devanagari 2.16x,
    emoji 1.36x — but masked text carrying `[EMAIL_A7]` pseudonyms is **0.75x** and a
    quoted reply is 0.95x. It under-counts worst on masked text, which is precisely what
    gets embedded, because bracketed pseudonyms tokenize into more pieces than
    four-characters-per-token predicts.

    That is safe at the shipped defaults and only because of headroom: 768 target against
    the provider's 2048 input limit means even a 0.75 ratio lands near 1024. **Raising
    `target_tokens` toward 2048 on the assumption that this over-counts would be a real
    bug**, and S6 rejects `truncated=true` responses as the backstop. See ADR 0009.

    The rule is four ASCII characters per token, plus one token per non-ASCII character.
    The second half matters more than it looks: a subword tokeniser typically emits at
    least one token per Devanagari or CJK character and often several, so a flat
    characters-over-four rule under-counts non-Latin text badly — which is precisely the
    direction that fails in production.

    S6 replaces this with the embedding model's own tokeniser. Until it does,
    `Chunk.token_count` is an estimate and must not be reported as anything else.
    """
    ascii_characters = sum(1 for character in text if character.isascii())
    non_ascii = len(text) - ascii_characters
    return -(-ascii_characters // 4) + non_ascii


# --------------------------------------------------------------------------- boundaries


class _Boundary(IntEnum):
    """How strong a split point is. Lower binds harder, so `min` picks the winner."""

    HEADING = 0
    PARAGRAPH = 1
    SENTENCE = 2
    #: Introduced by the hard token limit inside an oversized sentence. Never preferred.
    FORCED = 3


#: ATX headings only (`## Section`), and setext underlines are deliberately NOT detected.
#: In the pilot corpus a line of dashes under a line of text is overwhelmingly an email
#: separator rather than a heading — `-----Original Message-----` and its variants are
#: everywhere in Enron — so setext detection would invent headings in the middle of quoted
#: replies and fragment every thread. Under-detecting costs nothing: a missed heading is
#: just a weaker boundary, and packing still splits there if it needs to.
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+\S", re.MULTILINE)

#: One or more blank lines. The blank lines belong to the segment *before* the boundary.
_PARAGRAPH = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")

#: A terminator, optional closing quotes or brackets, then whitespace. The boundary is
#: placed after the whitespace, so separators attach to the preceding unit and the
#: segments stay contiguous with no gaps.
#:
#: Typographic quotes are spelled with `chr()` rather than pasted in. Real documents are
#: full of them so the pattern has to match them, but a curly quote in source is
#: indistinguishable from a straight one at a glance — which is why ruff refuses it, and
#: it is right to.
_CLOSERS: Final = "'\"" + chr(0x2019) + chr(0x201D) + ")]"
_SENTENCE = re.compile("[.!?][" + re.escape(_CLOSERS) + "]*(\\s+)")

#: What may legitimately start a sentence. `[` is in the list because a masked token —
#: `[EMAIL_A7]` — frequently does.
_SENTENCE_OPENERS: Final = frozenset("\"'([" + chr(0x2018) + chr(0x201C))

#: Words that end in a full stop without ending a sentence. Kept short and common rather
#: than exhaustive: every entry is a case where splitting would be wrong, and a missing
#: entry costs one over-eager split, not a correctness failure.
_ABBREVIATIONS: Final = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "rev", "hon",
        "inc", "ltd", "co", "corp", "plc", "llc", "dept", "univ",
        "vs", "etc", "eg", "ie", "al", "approx", "est", "no", "fig", "vol",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
        "mon", "tue", "wed", "thu", "fri", "sat", "sun",
        "am", "pm", "cf", "ca", "pp",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class _Segment:
    """An indivisible run of masked text, plus the strength of the boundary at its start.

    Segments are contiguous and cover the whole document: `segments[i].end ==
    segments[i + 1].start`. That is what makes the coverage guarantee provable rather
    than hopeful.
    """

    start: int
    end: int
    level: _Boundary


def _word_before(text: str, index: int) -> str:
    """The alphanumeric run ending immediately before `index`, lower-cased."""
    end = index
    start = end
    while start > 0 and text[start - 1].isalnum():
        start -= 1
    return text[start:end].lower()


def _is_sentence_break(text: str, terminator: int, boundary: int) -> bool:
    """Whether a terminator really ends a sentence.

    Three ways it does not: the word before it is an abbreviation, the word before it is
    a single capital (an initial, as in `J. Smith`), or what follows does not look like
    the start of anything. Decimals and version strings never reach here — the pattern
    requires whitespace after the terminator, and `3.14` has none.
    """
    if boundary >= len(text):
        return False

    following = text[boundary]
    if not (following.isupper() or following.isdigit() or following in _SENTENCE_OPENERS):
        return False

    if text[terminator] != ".":
        return True

    word = _word_before(text, terminator)
    if word in _ABBREVIATIONS:
        return False
    return not (len(word) == 1 and text[terminator - 1].isupper())


def _mark(marks: dict[int, _Boundary], position: int, level: _Boundary) -> None:
    """Record a boundary, keeping the strongest claim on that position."""
    existing = marks.get(position)
    marks[position] = level if existing is None else min(existing, level)


def _span_index(spans: Sequence[tuple[int, int]]) -> list[int]:
    return [start for start, _ in spans]


def _safe_boundary(position: int, spans: Sequence[tuple[int, int]], starts: list[int]) -> int:
    """Push a boundary out of any masked span it landed inside.

    Forward, always, so the adjustment cannot loop and cannot reorder two boundaries.

    In practice this almost never fires: a masked token is `[TYPE_XX]` over the Crockford
    alphabet, which contains no whitespace, no sentence terminator and no newline, so a
    natural boundary cannot land inside one. It fires for hard splits at the token limit,
    and it is applied to every boundary regardless — a rule enforced in one place is a
    rule, and a rule enforced where somebody remembered to is a hope.
    """
    index = bisect_right(starts, position) - 1
    if index < 0:
        return position
    start, end = spans[index]
    return end if start < position < end else position


def _boundaries(text: str, spans: Sequence[tuple[int, int]]) -> list[tuple[int, _Boundary]]:
    """Every split point in the document, strongest claim per position.

    Position 0 is always present, so the segment list covers the text from its first
    character. Positions at or past the end are dropped: the end of the document is where
    the last segment stops, not a place a new one begins.
    """
    marks: dict[int, _Boundary] = {0: _Boundary.HEADING}

    for match in _PARAGRAPH.finditer(text):
        _mark(marks, match.end(), _Boundary.PARAGRAPH)

    for match in _HEADING.finditer(text):
        _mark(marks, match.start(), _Boundary.HEADING)
        # The line *after* a heading is only a SENTENCE-strength boundary, deliberately.
        # Marking it HEADING would let a chunk close immediately after the heading line
        # and strand the heading in a chunk of its own, away from everything it titles.
        line_end = text.find("\n", match.start())
        if line_end != -1:
            _mark(marks, line_end + 1, _Boundary.SENTENCE)

    for match in _SENTENCE.finditer(text):
        boundary = match.end()
        if _is_sentence_break(text, match.start(), boundary):
            _mark(marks, boundary, _Boundary.SENTENCE)

    starts = _span_index(spans)
    adjusted: dict[int, _Boundary] = {}
    for position, level in marks.items():
        safe = _safe_boundary(position, spans, starts)
        if 0 <= safe < len(text):
            _mark(adjusted, safe, level)

    return sorted(adjusted.items())


# --------------------------------------------------------------------------- hard limit


def _budget_end(text: str, start: int, limit: int, target: int, count_tokens: TokenCounter) -> int:
    """The furthest position from `start` whose prefix still fits in `target` tokens.

    Exponential probe, then bisect. The probe is what keeps this linear overall: without
    it, finding the cut in a ten-megabyte unbroken string would count tokens over the
    whole remainder on every step. With it, each search costs work proportional to the
    piece it finds, so the total is proportional to the document.

    Deliberately no assumption that the counter is additive or even monotonic in any
    strong sense — it is only assumed non-decreasing in prefix length, which is true of
    every tokeniser and of the estimator.
    """
    if count_tokens(text[start:limit]) <= target:
        return limit

    low = start
    high = start + 1
    while high < limit and count_tokens(text[start:high]) <= target:
        low = high
        high = min(limit, start + (high - start) * 2)

    while low < high:
        middle = (low + high + 1) // 2
        if count_tokens(text[start:middle]) <= target:
            low = middle
        else:
            high = middle - 1
    return low


#: Code points that continue a grapheme cluster begun by the character before them.
#: Combining marks are found with `unicodedata.combining`; these are the ones it does not
#: report — the joiner, variation selectors, skin-tone modifiers and the keycap mark.
_ZWJ: Final = "‍"
_CLUSTER_CONTINUERS: Final = frozenset(
    {_ZWJ, "⃣"}
    | {chr(code) for code in range(0xFE00, 0xFE10)}
    | {chr(code) for code in range(0x1F3FB, 0x1F400)}
)


def _is_regional_indicator(character: str) -> bool:
    return "🇦" <= character <= "🇿"


def _continues_cluster(text: str, position: int) -> bool:
    """Whether cutting at `position` would split one user-perceived character."""
    character = text[position]
    if unicodedata.combining(character) != 0 or character in _CLUSTER_CONTINUERS:
        return True
    if text[position - 1] == _ZWJ:
        return True
    # A flag is a pair of regional indicators; cutting between them leaves two letters.
    return _is_regional_indicator(character) and _is_regional_indicator(text[position - 1])


def _cluster_safe(text: str, position: int, floor: int) -> int:
    """Move a cut back off the inside of a grapheme cluster.

    Backwards, never forwards, because forwards would exceed the token budget the cut was
    chosen to respect.

    Without this a hard split through unbroken non-Latin text hands the model a chunk
    beginning with a Devanagari virama, a zero-width joiner or a variation selector — a
    fragment of a character rather than a character. The offsets stay correct either way,
    so nothing downstream fails; the text simply reads as corrupted, in the one place
    nobody looks until a customer does. Found by sweeping budgets over Devanagari, a ZWJ
    emoji family and a keycap sequence.

    Gives up and returns the original position if it would have to walk back to `floor`,
    because a chunk that fails to advance is a worse outcome than a split cluster.
    """
    candidate = position
    while floor < candidate < len(text) and _continues_cluster(text, candidate):
        candidate -= 1
    return candidate if candidate > floor else position


def _last_break(text: str, low: int, high: int) -> int | None:
    """The last position in `(low, high]` that begins a word. None if there is none."""
    for index in range(high, low, -1):
        if index < len(text) and text[index - 1].isspace() and not text[index].isspace():
            return index
    return None


def _hard_cuts(
    text: str,
    segment: _Segment,
    target: int,
    count_tokens: TokenCounter,
    spans: Sequence[tuple[int, int]],
    starts: list[int],
) -> list[int]:
    """Split points inside one oversized segment.

    Only reached when a single sentence costs more than a whole chunk's budget on its own
    — §9.2's "hard token limit", and the one case where splitting mid-sentence is correct.
    Cuts land at word boundaries where there is one, and are pushed out of any masked span
    regardless.
    """
    cuts: list[int] = []
    cursor = segment.start
    while True:
        reach = _budget_end(text, cursor, segment.end, target, count_tokens)
        if reach >= segment.end:
            return cuts

        cut = _last_break(text, cursor, reach) or reach
        cut = _cluster_safe(text, cut, cursor)
        # Span safety is applied last and wins: a token split in half corrupts the offset
        # map, while a split cluster only looks wrong. The two never actually collide —
        # a masked token is ASCII from end to end — but the precedence is stated rather
        # than left to luck.
        cut = _safe_boundary(cut, spans, starts)
        # Progress guard. `_safe_boundary` only moves forward, so this can only fire if a
        # single masked span is longer than the entire token budget — which the real
        # detectors cannot produce, since a token is eleven characters.
        if cut <= cursor:
            cut = cursor + 1
        if cut >= segment.end:
            return cuts

        cuts.append(cut)
        cursor = cut


def _atomise(
    text: str,
    spans: Sequence[tuple[int, int]],
    target: int,
    count_tokens: TokenCounter,
) -> tuple[list[_Segment], list[int]]:
    """The document as contiguous, individually-packable segments, with their costs."""
    starts = _span_index(spans)
    marks = _boundaries(text, spans)
    positions = [position for position, _ in marks]
    levels = [level for _, level in marks]

    segments: list[_Segment] = []
    for index, position in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(text)
        segments.append(_Segment(position, end, levels[index]))

    expanded: list[_Segment] = []
    for segment in segments:
        if count_tokens(text[segment.start : segment.end]) <= target:
            expanded.append(segment)
            continue
        cursor = segment.start
        level = segment.level
        for cut in _hard_cuts(text, segment, target, count_tokens, spans, starts):
            expanded.append(_Segment(cursor, cut, level))
            cursor = cut
            level = _Boundary.FORCED
        expanded.append(_Segment(cursor, segment.end, level))

    costs = [count_tokens(text[segment.start : segment.end]) for segment in expanded]
    return expanded, costs


# --------------------------------------------------------------------------- packing


def _snap(
    segments: Sequence[_Segment],
    costs: Sequence[int],
    first: int,
    last: int,
    min_tokens: int,
) -> int:
    """Move a chunk's closing point back to the strongest boundary that still fills it.

    This is where the §9.2 hierarchy actually pays: given a chunk that could close
    anywhere in a range, close it at a heading in preference to a paragraph break, and at
    a paragraph break in preference to mid-paragraph. `min_tokens` is the floor that stops
    it snapping so far back that it emits a stub, and the tie-break is the *latest*
    qualifying boundary of the strongest level — so a document with a heading every few
    lines still fills its chunks rather than emitting one heading at a time.

    Two comparisons, not one, and the difference matters. Among the earlier candidates
    the test is `<=`, so a later boundary of equal strength wins and the chunk stays as
    full as it can. Against the greedy end it is `<`, so shrinking has to *buy* something:
    a candidate merely as strong as where the budget ran out is not a reason to give up
    the tokens between them. Written with one `<=` for both, the first qualifying
    boundary of any strength won every time and every chunk collapsed to `min_tokens`.
    """
    chosen: int | None = None
    chosen_level: _Boundary | None = None
    running = 0
    for candidate in range(first, last):
        running += costs[candidate]
        close = candidate + 1
        if close >= last or running < min_tokens:
            continue
        level = segments[close].level
        if chosen_level is None or level <= chosen_level:
            chosen_level = level
            chosen = close

    # `last < total` is the caller's precondition, so this boundary really exists.
    if chosen is not None and chosen_level is not None and chosen_level < segments[last].level:
        return chosen
    return last


def _overlap_start(costs: Sequence[int], first: int, last: int, overlap_tokens: int) -> int:
    """Where the next chunk begins, given how far back the overlap should reach.

    **Overlap is taken in whole segments and never splits one.** A segment costing more
    than the whole overlap budget is therefore not taken, and a document whose sentences
    are each longer than the budget gets no overlap at all. That is the rule, not a
    shortfall: manufacturing an overlap by cutting a sentence in half would break the one
    guarantee §9.2 asks for by name, and repeating a segment far larger than the budget
    would spend most of the next chunk restating the previous one.

    At the spec's defaults the budget is 115 tokens and ordinary prose runs 13 to 20
    tokens a sentence, so several fit. It is small targets that see no overlap, which is
    also where a whole sentence is a large fraction of a chunk and repeating one would
    cost the most.

    Never returns `first`, so the walk always advances. A single-segment chunk gets no
    overlap for the same reason: the only way to overlap it would be to repeat all of it.
    """
    if overlap_tokens <= 0:
        return last

    start = last
    accumulated = 0
    while start - 1 > first:
        candidate = accumulated + costs[start - 1]
        if candidate > overlap_tokens:
            break
        accumulated = candidate
        start -= 1
    return start


def _pack(
    text: str,
    segments: Sequence[_Segment],
    costs: Sequence[int],
    target: int,
    min_tokens: int,
    overlap_tokens: int,
    count_tokens: TokenCounter,
) -> list[tuple[int, int]]:
    """Segments into chunk ranges, in masked coordinates."""
    ranges: list[tuple[int, int]] = []
    total = len(segments)
    first = 0

    while first < total:
        # Greedy fill on the per-segment costs, which are computed once each.
        last = first + 1
        running = costs[first]
        while last < total and running + costs[last] <= target:
            running += costs[last]
            last += 1

        # Then one exact count of the joined text, because token counts are not additive
        # in general: a real tokeniser may merge across a boundary, and nothing here is
        # allowed to assume which direction that goes. Shrinks on the rare occasion the
        # join costs more than the parts. For the estimator and for every subword
        # tokeniser this loop does not run.
        while (
            last > first + 1
            and count_tokens(text[segments[first].start : segments[last - 1].end]) > target
        ):
            last -= 1

        if last < total:
            last = _snap(segments, costs, first, last, min_tokens)

        # Absorb a tail too short to stand on its own, but never at the cost of the
        # budget: a chunk over `target` is a chunk the embedding model rejects, whereas a
        # final chunk under `min_tokens` is merely a small one.
        if last < total and sum(costs[last:]) < min_tokens:
            whole = text[segments[first].start : segments[total - 1].end]
            if count_tokens(whole) <= target:
                last = total

        ranges.append((segments[first].start, segments[last - 1].end))
        if last >= total:
            break
        first = _overlap_start(costs, first, last, overlap_tokens)

    return ranges


# --------------------------------------------------------------------------- entry point


def chunk_document(
    masked: MaskResult,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
    min_tokens: int = DEFAULT_MIN_TOKENS,
    *,
    count_tokens: TokenCounter = estimate_tokens,
) -> list[Chunk]:
    """Split a masked document into embedding units carrying original-document offsets.

    Guarantees, all of them tested:

      * Every character of the document belongs to at least one chunk. Chunks overlap by
        design, so this is coverage, not a partition.
      * No chunk boundary falls strictly inside a `MaskedSpan`.
      * No sentence is split unless that sentence alone exceeds `target_tokens`.
      * `chunk.text` is masked text; `char_start` and `char_end` index the original.
      * No chunk exceeds `target_tokens` under the counter it was given.
      * Identical input yields an identical list, field for field.

    `overlap_ratio` is a budget, not a quota: overlap is taken in whole segments, so a
    document whose sentences each cost more than `target_tokens * overlap_ratio` gets no
    overlap rather than a split sentence. See `_overlap_start`.

    An empty document yields no chunks. Any non-empty document yields at least one, even
    if it is only whitespace — dropping it would put a hole in the coverage guarantee, and
    deciding that a document is not worth embedding belongs to the pipeline, not here.

    `count_tokens` defaults to `estimate_tokens`, which is an **estimate**. See the module
    docstring and ADR 0006 before persisting `token_count` anywhere.
    """
    if target_tokens < 1:
        raise ValueError("target_tokens must be at least 1")
    if not 0.0 <= overlap_ratio < 1.0:
        raise ValueError("overlap_ratio must be at least 0 and less than 1")
    if min_tokens < 1 or min_tokens > target_tokens:
        raise ValueError("min_tokens must be between 1 and target_tokens")

    text = masked.masked_text
    if not text:
        return []

    spans = [(span.masked_start, span.masked_end) for span in masked.spans]
    segments, costs = _atomise(text, spans, target_tokens, count_tokens)
    overlap_tokens = int(target_tokens * overlap_ratio)
    ranges = _pack(text, segments, costs, target_tokens, min_tokens, overlap_tokens, count_tokens)

    chunks: list[Chunk] = []
    for ordinal, (start, end) in enumerate(ranges):
        body = text[start:end]
        char_start, char_end = masked.original_range(start, end)
        chunks.append(
            Chunk(
                ordinal=ordinal,
                text=body,
                char_start=char_start,
                char_end=char_end,
                token_count=count_tokens(body),
            )
        )
    return chunks
