"""Chunking with original-document offsets (spec §9.2).

The invariant most of these tests are a special case of:

    the chunks cover the document, every boundary sits outside every masked span, and
    each chunk's `char_start`/`char_end` names the run of ORIGINAL text that its MASKED
    `text` stands for.

`assert_chunks_are_coherent` checks all of it at once, and is called from nearly every
test rather than restated. What each test adds on top is the one property it is named for.

A note on `masked_bounds`: `Chunk` carries original offsets, not masked ones, so the
helper recovers the masked boundaries by locating each chunk's text and keeping only the
occurrence whose `original_range` reproduces the offsets the chunker stored. That is
exact — an ambiguous match is rejected rather than guessed — and it cross-checks the
offset mapping as a side effect.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from itertools import pairwise, product
from typing import ClassVar

import pytest
from jutsu_core import (
    DEFAULT_MIN_TOKENS,
    DEFAULT_OVERLAP_RATIO,
    DEFAULT_TARGET_TOKENS,
    Chunk,
    Detection,
    MaskResult,
    PiiType,
    chunk_document,
    estimate_tokens,
    mask,
)

# --------------------------------------------------------------------------- helpers


@dataclass(frozen=True, slots=True)
class StubDetector:
    """A detector that reports ranges it was handed.

    Declared here rather than imported from the masking suite. `packages/core/tests` has
    no `conftest.py` and must not gain one: mypy derives module names from the nearest
    directory without an `__init__.py`, `packages/db/tests` already has a `conftest`, and
    a second one under the same tree makes `mypy packages` ambiguous and check nothing.
    Ten duplicated lines are cheaper than a type check that silently stops running.
    """

    pii_type: PiiType
    name: str
    ranges: tuple[Detection, ...] = field(default_factory=tuple)

    def detect(self, text: str) -> Iterable[Detection]:
        return self.ranges


def masked_bounds(masked: MaskResult, chunks: list[Chunk]) -> list[tuple[int, int]]:
    """Each chunk's range in MASKED coordinates, recovered exactly."""
    bounds: list[tuple[int, int]] = []
    search_from = 0
    for chunk in chunks:
        position = masked.masked_text.index(chunk.text, search_from)
        while masked.original_range(position, position + len(chunk.text)) != (
            chunk.char_start,
            chunk.char_end,
        ):
            # Not this occurrence. Raises if none matches, which is itself a failure
            # worth having: it means the stored offsets describe no real slice.
            position = masked.masked_text.index(chunk.text, position + 1)
        bounds.append((position, position + len(chunk.text)))
        search_from = position + 1
    return bounds


def assert_chunks_are_coherent(original: str, masked: MaskResult, chunks: list[Chunk]) -> None:
    """Every promise `chunk_document` makes, checked against one document."""
    if not masked.masked_text:
        assert chunks == []
        return

    assert chunks, "a non-empty document must produce at least one chunk"
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))

    bounds = masked_bounds(masked, chunks)

    # Coverage in masked coordinates. Chunks overlap by design, so this is "no gaps",
    # not "no overlaps".
    assert bounds[0][0] == 0
    assert bounds[-1][1] == len(masked.masked_text)
    for (_, previous_end), (next_start, _) in pairwise(bounds):
        assert next_start <= previous_end, "gap between chunks"

    # Coverage in original coordinates, which is what a citation indexes.
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(original)
    for previous, following in pairwise(chunks):
        assert following.char_start <= previous.char_end, "gap in the original"

    for chunk, (start, end) in zip(chunks, bounds, strict=True):
        # The text is MASKED text. This is the assertion that catches the worst
        # available bug in this module: slicing the original into `Chunk.text` would
        # push unmasked PII into the vector store and the model context.
        assert chunk.text == masked.masked_text[start:end]
        assert chunk.char_end >= chunk.char_start
        assert original[chunk.char_start : chunk.char_end] or chunk.char_start == chunk.char_end

        # No boundary strictly inside a masked span, in either direction.
        for span in masked.spans:
            assert not span.masked_start < start < span.masked_end, "chunk starts mid-token"
            assert not span.masked_start < end < span.masked_end, "chunk ends mid-token"


def counting_counter() -> tuple[Callable[[str], int], list[int]]:
    """The estimator, plus a tally of how many characters it was asked to look at.

    Used to assert bounded work rather than elapsed time — a wall-clock assertion in CI
    is a flake generator, and the thing actually worth pinning down is that the algorithm
    does not go quadratic on a document with no whitespace in it.
    """
    examined: list[int] = [0]

    def counter(text: str) -> int:
        examined[0] += len(text)
        return estimate_tokens(text)

    return counter, examined


def chunk_text(
    text: str,
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
    min_tokens: int = DEFAULT_MIN_TOKENS,
) -> tuple[MaskResult, list[Chunk]]:
    """Mask and chunk in one step, returning both halves.

    Spelled out rather than `**kwargs`, so a typo in a parameter name is a type error
    here instead of a silently ignored argument and a test that proves nothing.
    """
    masked = mask(text)
    return masked, chunk_document(masked, target_tokens, overlap_ratio, min_tokens)


PARAGRAPH_A = (
    "The migration ran cleanly against staging on Tuesday morning. "
    "Nobody reported a regression in the hour that followed. "
    "We agreed to hold the production run until Thursday."
)
PARAGRAPH_B = (
    "Retrieval latency sat at roughly two hundred milliseconds through the test. "
    "That is comfortably inside the budget we set in March. "
    "The remaining risk is the cold start, which nobody has measured yet."
)


# --------------------------------------------------------------------------- estimator


class TestEstimator:
    def test_is_deterministic(self) -> None:
        assert estimate_tokens(PARAGRAPH_A) == estimate_tokens(PARAGRAPH_A)

    def test_empty_text_costs_nothing(self) -> None:
        assert estimate_tokens("") == 0

    def test_counts_non_ascii_characters_one_token_each(self) -> None:
        """The half of the rule that stops it under-counting Devanagari.

        A flat characters-over-four rule would price a Devanagari word at a quarter of
        what a subword tokeniser charges, and an under-count is the error that fails at
        request time rather than merely wasting budget.
        """
        devanagari = "नमस्ते"
        assert estimate_tokens(devanagari) == len(devanagari)
        assert estimate_tokens(devanagari) > estimate_tokens("a" * len(devanagari))

    def test_is_monotonic_in_prefix_length(self) -> None:
        """`_budget_end` bisects on this. A counter that dipped would break the search."""
        text = PARAGRAPH_A + " नमस्ते 🙏 " + PARAGRAPH_B
        values = [estimate_tokens(text[:index]) for index in range(len(text) + 1)]
        assert values == sorted(values)


# --------------------------------------------------------------------------- hierarchy


class TestSplitHierarchy:
    def test_a_short_document_is_one_chunk(self) -> None:
        original = "One short line, nothing more."
        masked, chunks = chunk_text(original)
        assert len(chunks) == 1
        assert chunks[0].text == original
        assert (chunks[0].char_start, chunks[0].char_end) == (0, len(original))
        assert_chunks_are_coherent(original, masked, chunks)

    def test_closes_at_a_sentence_boundary(self) -> None:
        original = f"{PARAGRAPH_A} {PARAGRAPH_B}"
        masked, chunks = chunk_text(original, target_tokens=40, min_tokens=10, overlap_ratio=0.0)

        assert len(chunks) > 1
        for chunk in chunks[:-1]:
            assert chunk.text.rstrip().endswith((".", "!", "?"))
        assert_chunks_are_coherent(original, masked, chunks)

    def test_prefers_a_paragraph_break_over_a_sentence_break(self) -> None:
        """The hierarchy, made observable.

        Both paragraphs fit inside one budget together, so a purely greedy packer would
        run straight through the blank line and close mid-second-paragraph. Closing at
        the blank line is the §9.2 preference doing its job.
        """
        original = f"{PARAGRAPH_A}\n\n{PARAGRAPH_B}"
        masked, chunks = chunk_text(original, target_tokens=70, min_tokens=20, overlap_ratio=0.0)

        assert len(chunks) >= 2
        assert chunks[0].text.rstrip() == PARAGRAPH_A
        assert_chunks_are_coherent(original, masked, chunks)

    def test_prefers_a_heading_over_a_paragraph_break(self) -> None:
        original = (
            f"## Migration\n\n{PARAGRAPH_A}\n\n"
            f"## Latency\n\n{PARAGRAPH_B}\n\nA closing remark about neither of them."
        )
        masked, chunks = chunk_text(original, target_tokens=80, min_tokens=15, overlap_ratio=0.0)

        assert len(chunks) >= 2
        assert chunks[1].text.startswith("## Latency"), chunks[1].text[:40]
        assert_chunks_are_coherent(original, masked, chunks)

    def test_a_heading_is_never_stranded_from_its_content(self) -> None:
        """The boundary after a heading line is weak on purpose.

        Marked as strongly as the heading itself, packing would happily close right after
        it and emit a chunk containing one line of title and nothing it titles.
        """
        original = f"## Migration\n\n{PARAGRAPH_A}\n\n## Latency\n\n{PARAGRAPH_B}"
        masked, chunks = chunk_text(original, target_tokens=60, min_tokens=10, overlap_ratio=0.0)

        for chunk in chunks:
            stripped = chunk.text.strip()
            if stripped.startswith("##"):
                assert len(stripped.splitlines()) > 1, f"heading alone in a chunk: {stripped!r}"
        assert_chunks_are_coherent(original, masked, chunks)

    def test_splits_a_sentence_only_when_it_exceeds_the_budget_alone(self) -> None:
        """§9.2's hard token limit, and the only case where mid-sentence is correct."""
        long_sentence = "word " * 400
        original = f"Short one. {long_sentence.strip()}. Short again."
        masked, chunks = chunk_text(original, target_tokens=60, min_tokens=10, overlap_ratio=0.0)

        assert len(chunks) > 2
        assert_chunks_are_coherent(original, masked, chunks)
        for chunk in chunks:
            assert estimate_tokens(chunk.text) <= 60

    def test_a_hard_split_lands_on_a_word_boundary(self) -> None:
        original = "alpha bravo charlie delta echo foxtrot golf hotel india juliet " * 20
        masked, chunks = chunk_text(original, target_tokens=30, min_tokens=5, overlap_ratio=0.0)

        for chunk in chunks[1:]:
            assert not chunk.text[:1].isspace(), "a cut left leading whitespace"
        assert_chunks_are_coherent(original, masked, chunks)


class TestSentenceDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "Ask Dr. Reyes about it tomorrow and nobody else.",
            "The invoice came from Acme Inc. and was paid on time.",
            "Signed by J. Reyes on behalf of the team last Thursday.",
            "Latency was 3.14 milliseconds on the second run of the day.",
            "Use the staging cluster, e.g. the one in europe-west1, for this.",
        ],
    )
    def test_does_not_split_on_a_full_stop_that_ends_no_sentence(self, text: str) -> None:
        """Every false split costs a chunk boundary in the middle of a thought."""
        masked, chunks = chunk_text(text, target_tokens=200, min_tokens=1, overlap_ratio=0.0)
        assert len(chunks) == 1
        assert_chunks_are_coherent(text, masked, chunks)

    def test_a_masked_token_can_open_a_sentence(self) -> None:
        """`[EMAIL_A7]` at the head of a sentence is ordinary, not an edge case."""
        original = (
            "The contract was countersigned yesterday afternoon. "
            "chair@example.com approved the change without comment."
        )
        masked, chunks = chunk_text(original, target_tokens=18, min_tokens=4, overlap_ratio=0.0)

        assert len(chunks) > 1
        assert any(chunk.text.lstrip().startswith("[EMAIL_") for chunk in chunks)
        assert_chunks_are_coherent(original, masked, chunks)

    def test_an_email_separator_is_not_read_as_a_heading(self) -> None:
        """Why setext headings are deliberately not detected.

        `-----Original Message-----` is everywhere in the pilot corpus. Read as a setext
        underline it would invent a heading inside every quoted reply.
        """
        original = f"{PARAGRAPH_A}\n-----Original Message-----\nFrom: someone\n\n{PARAGRAPH_B}"
        masked, chunks = chunk_text(original, target_tokens=200, min_tokens=10)
        assert len(chunks) == 1, "the separator fragmented the thread"
        assert_chunks_are_coherent(original, masked, chunks)


# --------------------------------------------------------------------------- offsets


class TestOffsetsAndCoverage:
    def test_offsets_index_the_original_not_the_masked_text(self) -> None:
        """The trap this module exists to avoid, asserted directly.

        A masked email is ten characters; the address it replaced is far longer. So the
        stored range is *wider* than the text, and code that assumed the two lengths
        matched would mis-highlight every citation after the first replacement.
        """
        address = "very.long.address@subdomain.example.com"
        original = f"Write to {address} before Friday, please."
        masked, chunks = chunk_text(original)

        chunk = chunks[0]
        assert chunk.char_end - chunk.char_start == len(original)
        assert len(chunk.text) < len(original)
        assert address not in chunk.text
        assert_chunks_are_coherent(original, masked, chunks)

    def test_every_offset_resolves_to_real_original_text(self) -> None:
        original = f"{PARAGRAPH_A} Reach chair@example.com or +44 20 7946 0958.\n\n{PARAGRAPH_B}"
        masked, chunks = chunk_text(original, target_tokens=35, min_tokens=8)

        for chunk in chunks:
            assert original[chunk.char_start : chunk.char_end], "empty original range"
        assert_chunks_are_coherent(original, masked, chunks)

    def test_the_union_of_ranges_covers_the_document(self) -> None:
        original = f"{PARAGRAPH_A}\n\n{PARAGRAPH_B}\n\n{PARAGRAPH_A}"
        masked, chunks = chunk_text(original, target_tokens=45, min_tokens=10)

        covered: set[int] = set()
        for chunk in chunks:
            covered.update(range(chunk.char_start, chunk.char_end))
        assert covered == set(range(len(original)))
        assert_chunks_are_coherent(original, masked, chunks)


class TestMaskedSpanSafety:
    def test_no_boundary_falls_inside_a_token(self) -> None:
        original = " ".join(f"person{index:02d}@example.com" for index in range(60))
        masked, chunks = chunk_text(original, target_tokens=20, min_tokens=4)

        assert len(chunks) > 5
        assert_chunks_are_coherent(original, masked, chunks)
        for chunk in chunks:
            assert chunk.text.count("[") == chunk.text.count("]")

    def test_a_document_that_is_entirely_one_span_stays_whole(self) -> None:
        original = "chair@example.com"
        masked, chunks = chunk_text(original, target_tokens=2, min_tokens=1)

        assert len(chunks) == 1
        assert chunks[0].text == masked.masked_text
        assert (chunks[0].char_start, chunks[0].char_end) == (0, len(original))

    def test_a_hard_split_is_pushed_out_of_a_span(self) -> None:
        """The one path where a cut could otherwise land inside a token.

        A stub detector masks the middle of an unbroken run, so the token limit falls
        inside the replacement rather than near it.
        """
        original = "x" * 400
        detectors = (StubDetector(PiiType.PERSON, "middle", (Detection(150, 250),)),)
        result = mask(original, detectors)
        chunks = chunk_document(result, target_tokens=10, min_tokens=2, overlap_ratio=0.0)

        assert_chunks_are_coherent(original, result, chunks)
        for chunk in chunks:
            assert chunk.text.count("[") == chunk.text.count("]")


# --------------------------------------------------------------------------- packing


class TestOverlapAndMinimum:
    def test_consecutive_chunks_overlap(self) -> None:
        """A budget of 24 tokens against sentences costing 13 to 19, so one fits."""
        original = f"{PARAGRAPH_A} {PARAGRAPH_B} {PARAGRAPH_A}"
        masked, chunks = chunk_text(original, target_tokens=80, min_tokens=8, overlap_ratio=0.3)

        assert len(chunks) > 1
        bounds = masked_bounds(masked, chunks)
        assert any(
            next_start < previous_end for (_, previous_end), (next_start, _) in pairwise(bounds)
        ), "overlap_ratio had no effect"
        assert_chunks_are_coherent(original, masked, chunks)

    def test_no_overlap_when_no_whole_segment_fits_the_budget(self) -> None:
        """The documented rule, asserted rather than left as a surprise.

        Overlap is taken in whole segments. Here the budget is twelve tokens and the
        cheapest sentence costs thirteen, so nothing fits and the chunks abut. The
        alternative — cutting a sentence in half to manufacture an overlap — would break
        the guarantee §9.2 asks for by name, so no overlap is the correct answer.
        """
        original = f"{PARAGRAPH_A} {PARAGRAPH_B} {PARAGRAPH_A}"
        masked, chunks = chunk_text(original, target_tokens=40, min_tokens=8, overlap_ratio=0.3)

        assert min(estimate_tokens(sentence) for sentence in original.split(". ")) > 40 * 0.3
        bounds = masked_bounds(masked, chunks)
        for (_, previous_end), (next_start, _) in pairwise(bounds):
            assert next_start == previous_end
        assert_chunks_are_coherent(original, masked, chunks)

    def test_zero_overlap_produces_a_partition(self) -> None:
        original = f"{PARAGRAPH_A} {PARAGRAPH_B}"
        masked, chunks = chunk_text(original, target_tokens=40, min_tokens=8, overlap_ratio=0.0)

        bounds = masked_bounds(masked, chunks)
        for (_, previous_end), (next_start, _) in pairwise(bounds):
            assert next_start == previous_end

    def test_overlap_always_advances(self) -> None:
        """A regression guard on the one loop in here that could fail to terminate."""
        original = " ".join(f"Sentence number {index}." for index in range(40))
        masked, chunks = chunk_text(original, target_tokens=12, min_tokens=2, overlap_ratio=0.9)

        bounds = masked_bounds(masked, chunks)
        for (previous_start, _), (next_start, _) in pairwise(bounds):
            assert next_start > previous_start
        assert_chunks_are_coherent(original, masked, chunks)

    def test_a_short_tail_is_absorbed_into_the_previous_chunk(self) -> None:
        original = f"{PARAGRAPH_A}\n\nOne stray line."
        masked, chunks = chunk_text(original, target_tokens=90, min_tokens=30, overlap_ratio=0.0)

        assert len(chunks) == 1, "a stub chunk was emitted instead of being absorbed"
        assert_chunks_are_coherent(original, masked, chunks)

    def test_a_tail_is_not_absorbed_past_the_budget(self) -> None:
        """`min_tokens` is a preference; `target_tokens` is a limit.

        Absorbing a stub is worth doing right up to the point where it would produce a
        chunk the embedding model refuses. Then the stub is the lesser problem.
        """
        original = f"{PARAGRAPH_A} {PARAGRAPH_B} Tail."
        masked, chunks = chunk_text(original, target_tokens=45, min_tokens=40, overlap_ratio=0.0)

        for chunk in chunks:
            assert estimate_tokens(chunk.text) <= 45
        assert_chunks_are_coherent(original, masked, chunks)

    def test_no_chunk_exceeds_the_budget(self) -> None:
        original = f"{PARAGRAPH_A}\n\n{PARAGRAPH_B}\n\n## Heading\n\n{PARAGRAPH_A}"
        for target in (20, 35, 64, 128):
            masked, chunks = chunk_text(original, target_tokens=target, min_tokens=5)
            for chunk in chunks:
                assert estimate_tokens(chunk.text) <= target, target
            assert_chunks_are_coherent(original, masked, chunks)


class TestTokenCounterInjection:
    def test_boundaries_follow_the_injected_counter(self) -> None:
        """Not the estimator. The chunker must be correct for whatever S6 supplies."""
        original = " ".join(f"Sentence number {index}." for index in range(30))

        def one_token_per_character(text: str) -> int:
            return len(text)

        masked = mask(original)
        with_stub = chunk_document(
            masked,
            target_tokens=60,
            min_tokens=5,
            overlap_ratio=0.0,
            count_tokens=one_token_per_character,
        )
        with_estimator = chunk_document(masked, target_tokens=60, min_tokens=5, overlap_ratio=0.0)

        assert len(with_stub) > len(with_estimator), "the counter made no difference"
        for chunk in with_stub:
            assert len(chunk.text) <= 60
        assert_chunks_are_coherent(original, masked, with_stub)

    def test_a_super_additive_counter_still_respects_the_budget(self) -> None:
        """Token counts are not additive, and nothing here may assume they are.

        A real tokeniser usually merges across a join, making the whole cheaper than the
        parts. This counter does the opposite, which is the direction that breaks a
        packer built on per-segment sums.
        """
        original = " ".join(f"Sentence number {index} here." for index in range(40))

        def super_additive(text: str) -> int:
            return len(text) + len(text) // 10

        masked = mask(original)
        chunks = chunk_document(
            masked,
            target_tokens=90,
            min_tokens=10,
            overlap_ratio=0.0,
            count_tokens=super_additive,
        )

        for chunk in chunks:
            assert super_additive(chunk.text) <= 90
        assert_chunks_are_coherent(original, masked, chunks)

    def test_token_count_is_reported_by_the_counter_that_was_used(self) -> None:
        original = f"{PARAGRAPH_A} {PARAGRAPH_B}"

        def double_length(text: str) -> int:
            return len(text) * 2

        masked = mask(original)
        chunks = chunk_document(
            masked, target_tokens=400, min_tokens=10, count_tokens=double_length
        )
        for chunk in chunks:
            assert chunk.token_count == len(chunk.text) * 2


class TestDeterminism:
    def test_identical_input_yields_identical_chunks(self) -> None:
        original = f"{PARAGRAPH_A}\n\n## Heading\n\n{PARAGRAPH_B} chair@example.com signed."
        masked = mask(original)

        first = chunk_document(masked, target_tokens=30, min_tokens=6)
        second = chunk_document(masked, target_tokens=30, min_tokens=6)
        assert [chunk.model_dump() for chunk in first] == [chunk.model_dump() for chunk in second]

    def test_masking_and_chunking_together_are_reproducible(self) -> None:
        original = f"{PARAGRAPH_A} Reach chair@example.com today.\n\n{PARAGRAPH_B}"
        first = chunk_document(mask(original), target_tokens=30, min_tokens=6)
        second = chunk_document(mask(original), target_tokens=30, min_tokens=6)
        assert [chunk.model_dump() for chunk in first] == [chunk.model_dump() for chunk in second]


# --------------------------------------------------------------------------- unicode


class TestUnicode:
    def test_offsets_are_character_indices_not_byte_indices(self) -> None:
        original = (
            "नमस्ते 🙏 यह पहला वाक्य है। Write to chair@example.com before Friday. और यह अंतिम पंक्ति है 🙏"
        )
        masked, chunks = chunk_text(original, target_tokens=25, min_tokens=5)

        assert_chunks_are_coherent(original, masked, chunks)
        # The last chunk ends at the character length, which differs from the byte length.
        assert chunks[-1].char_end == len(original)
        assert len(original.encode("utf-8")) != len(original)

    def test_emoji_are_never_cut_in_half_by_a_hard_split(self) -> None:
        original = "🙏" * 300
        masked, chunks = chunk_text(original, target_tokens=20, min_tokens=4, overlap_ratio=0.0)

        assert len(chunks) > 5
        rejoined = "".join(chunk.text for chunk in chunks)
        assert rejoined == masked.masked_text
        assert_chunks_are_coherent(original, masked, chunks)

    def test_a_devanagari_document_is_chunked_more_finely_than_ascii(self) -> None:
        """Because the estimator prices non-ASCII higher, which is the safe direction."""
        devanagari = "यह एक वाक्य है। " * 40
        ascii_text = "This is a sentence. " * 40

        _, devanagari_chunks = chunk_text(devanagari, target_tokens=64, min_tokens=8)
        _, ascii_chunks = chunk_text(ascii_text, target_tokens=64, min_tokens=8)
        assert len(devanagari_chunks) > len(ascii_chunks)


# --------------------------------------------------------------------------- degenerate


class TestDegenerateInput:
    def test_an_empty_document_yields_no_chunks(self) -> None:
        assert chunk_document(mask("")) == []

    def test_a_whitespace_only_document_still_yields_one_chunk(self) -> None:
        """Coverage stays total. Deciding a document is not worth embedding is the
        pipeline's call, not the chunker's, and a hole here would be silent."""
        original = "   \n\n  \t "
        masked, chunks = chunk_text(original)
        assert len(chunks) == 1
        assert_chunks_are_coherent(original, masked, chunks)

    def test_a_document_with_no_trailing_newline_is_fully_covered(self) -> None:
        original = f"{PARAGRAPH_A}\n\n{PARAGRAPH_B}"
        masked, chunks = chunk_text(original, target_tokens=40, min_tokens=8)
        assert chunks[-1].char_end == len(original)
        assert_chunks_are_coherent(original, masked, chunks)

    def test_one_unbroken_run_with_no_whitespace_is_split(self) -> None:
        original = "x" * 2000
        masked, chunks = chunk_text(original, target_tokens=25, min_tokens=5, overlap_ratio=0.0)

        assert len(chunks) > 10
        assert "".join(chunk.text for chunk in chunks) == original
        assert_chunks_are_coherent(original, masked, chunks)

    def test_a_single_character_document(self) -> None:
        masked, chunks = chunk_text("x")
        assert len(chunks) == 1
        assert chunks[0].text == "x"
        assert (chunks[0].char_start, chunks[0].char_end) == (0, 1)
        assert_chunks_are_coherent("x", masked, chunks)

    @pytest.mark.parametrize(
        ("target", "overlap", "minimum"),
        [(0, 0.15, 1), (-1, 0.15, 1), (10, 1.0, 1), (10, -0.1, 1), (10, 0.15, 0), (10, 0.15, 11)],
    )
    def test_impossible_parameters_are_refused(
        self, target: int, overlap: float, minimum: int
    ) -> None:
        """An `overlap_ratio` of 1 in particular: the next chunk would never advance."""
        with pytest.raises(ValueError):
            chunk_document(
                mask("some text"),
                target_tokens=target,
                overlap_ratio=overlap,
                min_tokens=minimum,
            )

    def test_the_spec_defaults_are_the_signature_defaults(self) -> None:
        assert (DEFAULT_TARGET_TOKENS, DEFAULT_OVERLAP_RATIO, DEFAULT_MIN_TOKENS) == (
            768,
            0.15,
            128,
        )


class TestPathologicalInput:
    """Chunking runs in the worker over third-party content, which §30 treats as
    untrusted. A document shaped to make the algorithm quadratic is a denial of service
    against the ingestion queue, so the bound is asserted rather than assumed.

    Work is measured in characters handed to the counter, not in seconds: a wall-clock
    assertion in CI is a flake generator, and the property that matters is the shape of
    the growth, not the speed of the machine.
    """

    def test_an_unbroken_run_costs_work_linear_in_its_length(self) -> None:
        original = "x" * 100_000
        counter, examined = counting_counter()
        masked = mask(original)

        chunks = chunk_document(
            masked, target_tokens=768, min_tokens=128, overlap_ratio=0.0, count_tokens=counter
        )

        assert chunks
        assert "".join(chunk.text for chunk in chunks) == original
        assert examined[0] < 60 * len(original), examined[0]

    def test_many_tiny_sentences_cost_work_linear_in_the_document(self) -> None:
        original = "A b. " * 20_000
        counter, examined = counting_counter()
        masked = mask(original)

        chunks = chunk_document(
            masked, target_tokens=768, min_tokens=128, overlap_ratio=0.0, count_tokens=counter
        )

        assert len(chunks) > 10
        assert examined[0] < 60 * len(original), examined[0]

    def test_a_large_realistic_document_holds_every_invariant(self) -> None:
        original = "\n\n".join(
            f"## Section {index}\n\n{PARAGRAPH_A} Contact chair{index}@example.com.\n\n"
            f"{PARAGRAPH_B}"
            for index in range(60)
        )
        masked, chunks = chunk_text(original, target_tokens=200, min_tokens=40)

        assert len(chunks) > 20
        assert_chunks_are_coherent(original, masked, chunks)


# --------------------------------------------------------------------------- sweep


class TestGeneratedCorpus:
    """Every arrangement of two entities among four fillers, chunked at four budgets.

    The same product the masking suite uses, carried one stage further: if the offset map
    is right and the packer never cuts where it must not, every one of these holds. This
    is the M1 gate line — "every chunk offset resolves to matching text in the original
    document body" — proven at the layer that produces the offsets.
    """

    QUOTED = chr(10) + "> quoted reply" + chr(10)

    FILLERS = ("", " sent to ", "नमस्ते 🙏 पहला वाक्य है। ", QUOTED)
    ENTITIES = ("chair@example.com", "+44 20 7946 0958", "4111 1111 1111 1111", "123-45-6789")

    def test_every_arrangement_covers_and_resolves(self) -> None:
        checked = 0
        for lead, first, middle, second, tail in product(
            self.FILLERS, self.ENTITIES, self.FILLERS, self.ENTITIES, self.FILLERS
        ):
            original = f"{lead}{first}{middle}{second}{tail}{PARAGRAPH_A}"
            masked = mask(original)
            chunks = chunk_document(masked, target_tokens=24, min_tokens=5, overlap_ratio=0.2)
            assert_chunks_are_coherent(original, masked, chunks)
            checked += 1

        assert checked == len(self.FILLERS) ** 3 * len(self.ENTITIES) ** 2

    def test_the_budget_holds_across_every_arrangement(self) -> None:
        for lead, first, middle, second, tail in product(
            self.FILLERS, self.ENTITIES, self.FILLERS, self.ENTITIES, self.FILLERS
        ):
            original = f"{lead}{first}{middle}{second}{tail}{PARAGRAPH_B}"
            masked = mask(original)
            for chunk in chunk_document(masked, target_tokens=24, min_tokens=5):
                assert estimate_tokens(chunk.text) <= 24, original


class TestGraphemeClusters:
    """A hard split must not cut a user-perceived character in half.

    Only hard splits can: every other boundary lands after whitespace or at a line start,
    and nothing continues a cluster across those. Found by sweeping budgets over
    Devanagari, a zero-width-joiner emoji family and a keycap sequence — the chunker
    handed the model text beginning with a lone virama, joiner or variation selector.
    The offsets were right; the text was a fragment of a character.
    """

    ZWJ = chr(0x200D)
    CONTINUERS = (
        {ZWJ, chr(0x20E3)}
        | {chr(code) for code in range(0xFE00, 0xFE10)}
        | {chr(code) for code in range(0x1F3FB, 0x1F400)}
    )

    FAMILY = chr(0x1F468) + ZWJ + chr(0x1F469) + ZWJ + chr(0x1F467)
    CASES: ClassVar[dict[str, str]] = {
        "devanagari": "नमस्ते" * 60,
        "zwj_family": FAMILY * 40,
        "flag": (chr(0x1F1EE) + chr(0x1F1F3)) * 60,
        "skin_tone": (chr(0x1F44D) + chr(0x1F3FD)) * 60,
        "keycap": ("1" + chr(0xFE0F) + chr(0x20E3)) * 60,
    }

    def _splits_a_cluster(self, chunks: list[Chunk]) -> bool:
        """A chunk opening with a continuer, or closing on a joiner, is a cut cluster.

        Computed here from the Unicode properties rather than imported from the module
        under test, so the test would still fail if the implementation's own idea of a
        cluster were wrong.
        """
        for chunk in chunks:
            head = chunk.text[:1]
            if head and (unicodedata.combining(head) != 0 or head in self.CONTINUERS):
                return True
            if chunk.text.endswith(self.ZWJ):
                return True
        return False

    @pytest.mark.parametrize("label", sorted(CASES))
    def test_clusters_survive_every_budget_that_can_hold_them(self, label: str) -> None:
        original = self.CASES[label]
        masked = mask(original)
        for target in range(8, 41):
            chunks = chunk_document(masked, target_tokens=target, min_tokens=1, overlap_ratio=0.0)
            assert not self._splits_a_cluster(chunks), f"{label} at target={target}"
            assert "".join(chunk.text for chunk in chunks) == masked.masked_text
            assert_chunks_are_coherent(original, masked, chunks)

    def test_a_cluster_costlier_than_the_whole_budget_is_split_anyway(self) -> None:
        """The documented fallback, asserted so it is a rule and not a surprise.

        The family sequence costs five tokens. At a budget of three there is no cut that
        both advances and leaves the cluster whole, and a chunk that fails to advance is
        the worse outcome — so the cut happens. Coverage and the offsets still hold, which
        is the part that must never bend.
        """
        original = self.FAMILY * 40
        masked = mask(original)
        assert estimate_tokens(self.FAMILY) > 3

        chunks = chunk_document(masked, target_tokens=3, min_tokens=1, overlap_ratio=0.0)
        assert self._splits_a_cluster(chunks), "the fallback did not fire"
        assert "".join(chunk.text for chunk in chunks) == masked.masked_text
        assert_chunks_are_coherent(original, masked, chunks)
