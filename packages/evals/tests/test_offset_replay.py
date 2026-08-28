"""The offset clause, end to end on real pipeline output, plus a sabotage.

M1 says "every chunk offset resolves to matching original text". `remask_slice` is what
answers that, and this file runs it against the actual output of `mask` and
`chunk_document` — the same two functions `persist_document` composes, called the same
way, with the document id as the mask namespace.

No database here on purpose. What is under test is the arithmetic, and the arithmetic is
identical whether the numbers came from a row or from the chunker that produced them.
The database-backed version of this same property lives in
`test_gate_against_postgres.py`.

**The sabotage tests are the load-bearing ones.** A check that reports zero mismatches
against correct data is indistinguishable from a check that always reports zero, and the
whole symptom of the second one is silence.
"""

from __future__ import annotations

import uuid

import pytest
from jutsu_core.chunking import chunk_document
from jutsu_core.models import Chunk, MaskResult
from jutsu_core.pii import mask
from jutsu_evals.phase1 import remask_slice

#: Bodies chosen to exercise what the clause is actually about: masking that changes
#: string length (an email pseudonym is a different width from the address), several
#: distinct entities so the offset map has more than one step in it, and non-ASCII so a
#: byte/character confusion cannot pass.
BODIES = {
    "plain": (
        "Kicking off the Raptor review on Tuesday. Ping me at k.lay@enron.example "
        "or on +1 713 555 0142 if the numbers look wrong. "
        "Sherron said the SPE structure needs a second opinion before we sign."
    ),
    "repeated-entity": (
        "Forwarded from j.skilling@enron.example to j.skilling@enron.example. "
        "The same address twice must collapse to one pseudonym, and the second "
        "occurrence must still land at its own offset. Card 4111 1111 1111 1111 "
        "is in the test fixture and is not a real card."
    ),
    "unicode": (
        "प्रोजेक्ट की समीक्षा सोमवार को है — कृपया अपडेट भेजें। "
        "Contact 👨‍👩‍👧‍👦 the family alias at ops@enron.example, "
        "or call +44 20 7946 0958. देवनागरी में यह पाठ जानबूझकर लंबा है ताकि "
        "चंकर को कम से कम एक सीमा चुननी पड़े। " * 4
    ),
    "long": (
        "Decision: we will standardise on PostgreSQL for the primary store. "
        "Alternatives considered were MongoDB and DynamoDB. Owner is finance-ops "
        "at ap@enron.example. " * 40
    ),
}


@pytest.mark.parametrize("label", sorted(BODIES))
class TestReplayAgreesWithThePipeline:
    def test_every_stored_offset_resolves_to_the_stored_chunk_text(self, label: str) -> None:
        body = BODIES[label]
        document_id = str(uuid.uuid4())
        masked = mask(body, namespace=document_id)
        chunks = chunk_document(masked)

        assert chunks, "the fixture produced no chunks, so this proves nothing"
        for chunk in chunks:
            replayed = remask_slice(body, masked.spans, chunk.char_start, chunk.char_end)
            assert replayed == chunk.text, (
                f"chunk {chunk.ordinal} of {label}: the original text at "
                f"[{chunk.char_start}, {chunk.char_end}) does not mask to the chunk"
            )

    def test_offsets_stay_inside_the_original_body(self, label: str) -> None:
        body = BODIES[label]
        masked = mask(body, namespace="ns")
        for chunk in chunk_document(masked):
            assert 0 <= chunk.char_start <= chunk.char_end <= len(body)


class TestSabotage:
    """Break the property on purpose; the comparison must notice."""

    @pytest.fixture
    def sabotage_target(self) -> tuple[str, MaskResult, Chunk]:
        body = BODIES["plain"]
        masked = mask(body, namespace="ns")
        chunks = chunk_document(masked)
        return body, masked, chunks[0]

    def test_a_one_character_shift_is_caught(
        self, sabotage_target: tuple[str, MaskResult, Chunk]
    ) -> None:
        """The off-by-one that mis-highlights a citation is the whole point."""
        body, masked, chunk = sabotage_target
        shifted = remask_slice(body, masked.spans, chunk.char_start + 1, chunk.char_end)
        assert shifted != chunk.text

    def test_a_shifted_end_is_caught(self, sabotage_target: tuple[str, MaskResult, Chunk]) -> None:
        body, masked, chunk = sabotage_target
        assert remask_slice(body, masked.spans, chunk.char_start, chunk.char_end - 1) != chunk.text

    def test_storing_masked_coordinates_instead_of_original_ones_is_caught(self) -> None:
        """The §22 trap, made into a test.

        Storing the *masked* offsets on the row is the specific mistake CLAUDE.md warns
        about, and it is invisible until a citation highlights the wrong span. Here the
        two coordinate systems genuinely differ, so using the wrong one produces text
        that does not match.
        """
        body = BODIES["plain"]
        masked = mask(body, namespace="ns")
        assert masked.spans, "the fixture masked nothing, so the two systems coincide"

        chunk = chunk_document(masked)[0]
        masked_start = masked.to_masked(chunk.char_start)
        masked_end = masked.to_masked(chunk.char_end)
        # Only meaningful when masking actually shifted this chunk's coordinates.
        if (masked_start, masked_end) != (chunk.char_start, chunk.char_end):
            assert remask_slice(body, masked.spans, masked_start, masked_end) != chunk.text

    def test_dropping_the_mask_entirely_is_caught(self) -> None:
        """Raw original text where masked text belongs — an ADR 0005 violation."""
        body = BODIES["plain"]
        masked = mask(body, namespace="ns")
        chunk = chunk_document(masked)[0]
        raw = body[chunk.char_start : chunk.char_end]
        assert raw != chunk.text, "the fixture contains no PII, so this proves nothing"
        assert remask_slice(body, masked.spans, chunk.char_start, chunk.char_end) == chunk.text


class TestRemaskSliceItself:
    def test_a_slice_with_no_spans_is_plain_slicing(self) -> None:
        assert remask_slice("hello world", [], 0, 5) == "hello"

    def test_a_span_entirely_outside_the_slice_is_ignored(self) -> None:
        body = "call ops@enron.example now"
        masked = mask(body, namespace="ns")
        assert masked.spans
        span = masked.spans[0]
        assert remask_slice(body, masked.spans, span.orig_end, len(body)) == body[span.orig_end :]
