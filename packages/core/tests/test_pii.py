"""PII masking and the offset map (spec §9.1).

§9.1 names ten cases and requires them written before the implementation. They are the
ten methods in `TestRequiredCases`, each named for the case it covers, so a reader can
check the list against the spec without reading the bodies. Everything after that class
is the surrounding contract: detector precision, determinism, and the invariants the
chunker will lean on in S5.

The invariant worth stating once, because most of these tests are a special case of it:

    replacing every span's token in `masked_text` with the original text it stands for
    reproduces the original document exactly, character for character.

If that holds, the offsets are right. If it does not, some citation somewhere highlights
the wrong words — which is a visible product bug, not a rounding error.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from itertools import pairwise, product

import pytest
from jutsu_core import (
    CARD_DETECTOR,
    DEFAULT_DETECTORS,
    EMAIL_DETECTOR,
    IBAN_DETECTOR,
    PHONE_DETECTOR,
    SSN_DETECTOR,
    Detection,
    MaskResult,
    PiiDetector,
    PiiType,
    canonicalise,
    mask,
)

# --------------------------------------------------------------------------- helpers


@dataclass(frozen=True, slots=True)
class StubDetector:
    """A detector that reports ranges it was handed.

    Used wherever the case under test is about `mask`'s own behaviour — adjacency,
    overlap resolution, token reuse — rather than about whether a regex fires. Driving
    those through a real pattern would make the test depend on an accident of the
    pattern, and it would leave the PERSON path (which has no shipped detector, see
    ADR 0005) untestable.
    """

    pii_type: PiiType
    name: str
    ranges: tuple[Detection, ...] = field(default_factory=tuple)

    def detect(self, text: str) -> Iterable[Detection]:
        return self.ranges


def reconstruct(original: str, result: MaskResult) -> str:
    """Rebuild the original from the masked text plus the span map.

    The single strongest assertion available: it exercises both coordinate systems at
    once, and it fails on an off-by-one anywhere in either of them.
    """
    pieces: list[str] = []
    masked_cursor = 0
    for span in result.spans:
        pieces.append(result.masked_text[masked_cursor : span.masked_start])
        pieces.append(original[span.orig_start : span.orig_end])
        masked_cursor = span.masked_end
    pieces.append(result.masked_text[masked_cursor:])
    return "".join(pieces)


def assert_spans_are_coherent(original: str, result: MaskResult) -> None:
    """Every promise `MaskResult` makes, checked against one masking."""
    previous_orig_end = 0
    previous_masked_end = 0
    for span in result.spans:
        assert span.orig_start >= previous_orig_end, "spans overlap in the original"
        assert span.masked_start >= previous_masked_end, "spans overlap in the masked text"
        assert result.masked_text[span.masked_start : span.masked_end] == span.token
        assert original[span.orig_start : span.orig_end] != ""
        previous_orig_end = span.orig_end
        previous_masked_end = span.masked_end

    assert previous_orig_end <= len(original)
    assert previous_masked_end <= len(result.masked_text)
    assert reconstruct(original, result) == original


def masked_positions(result: MaskResult) -> set[int]:
    """Offsets that fall strictly inside a token, where translation is defined but lossy."""
    return {
        offset for span in result.spans for offset in range(span.masked_start + 1, span.masked_end)
    }


def original_positions(result: MaskResult) -> set[int]:
    return {offset for span in result.spans for offset in range(span.orig_start + 1, span.orig_end)}


LONG_EMAIL = "very.long.address@subdomain.example.com"
SHORT_EMAIL = "a@b.co"


# --------------------------------------------------------------------------- §9.1 list


class TestRequiredCases:
    """The ten cases §9.1 requires, in the order the spec lists them."""

    def test_1_no_pii_is_the_identity_case(self) -> None:
        text = "The quarterly review is on Thursday and nothing here is sensitive."
        result = mask(text)
        assert result.masked_text == text
        assert result.spans == []
        # With no spans the two coordinate systems are the same one.
        assert [result.to_original(i) for i in range(len(text) + 1)] == list(range(len(text) + 1))

    def test_2_replacement_shorter_than_the_original(self) -> None:
        text = f"Write to {LONG_EMAIL} before Friday."
        result = mask(text)

        assert len(result.masked_text) < len(text)
        assert LONG_EMAIL not in result.masked_text
        assert_spans_are_coherent(text, result)
        # Everything after the replacement shifted left by the length difference.
        tail = text.index(" before")
        assert result.to_original(result.to_masked(tail)) == tail

    def test_3_replacement_longer_than_the_original(self) -> None:
        text = f"Write to {SHORT_EMAIL} before Friday."
        result = mask(text)

        assert len(result.masked_text) > len(text)
        assert_spans_are_coherent(text, result)

    def test_4_two_adjacent_replacements(self) -> None:
        """No gap between them, in either coordinate system.

        The case that breaks a naive implementation carrying a running delta: with
        nothing between the spans there is no text to copy, and an implementation that
        assumes there always is emits an empty slice at the wrong offset.
        """
        text = "AAAABBBBBB tail"
        detectors: tuple[PiiDetector, ...] = (
            StubDetector(PiiType.PERSON, "left", (Detection(0, 4),)),
            StubDetector(PiiType.EMAIL, "right", (Detection(4, 10),)),
        )
        result = mask(text, detectors)

        first, second = result.spans
        assert first.orig_end == second.orig_start
        assert first.masked_end == second.masked_start
        assert result.masked_text == f"{first.token}{second.token} tail"
        assert_spans_are_coherent(text, result)

    def test_5_pii_at_index_zero(self) -> None:
        text = f"{LONG_EMAIL} sent the contract."
        result = mask(text)

        span = result.spans[0]
        assert span.orig_start == 0
        assert span.masked_start == 0
        assert result.to_original(0) == 0
        assert_spans_are_coherent(text, result)

    def test_6_pii_touching_the_end(self) -> None:
        text = f"Reply to {LONG_EMAIL}"
        result = mask(text)

        span = result.spans[-1]
        assert span.orig_end == len(text)
        assert span.masked_end == len(result.masked_text)
        # The end offset is not inside the token; it is the position after it.
        assert result.to_original(len(result.masked_text)) == len(text)
        assert_spans_are_coherent(text, result)

    def test_7_same_entity_three_times_is_one_token_and_three_spans(self) -> None:
        address = "chair@example.com"
        text = f"{address} opened it, {address} closed it, ask {address}."
        result = mask(text)

        assert len(result.spans) == 3
        assert len({span.token for span in result.spans}) == 1
        # One entity, one vault entry — the vault is keyed by value, not by occurrence.
        assert len({span.vault_key for span in result.spans}) == 1
        assert result.masked_text.count(result.spans[0].token) == 3
        assert_spans_are_coherent(text, result)

    def test_7b_case_and_formatting_variants_are_one_entity(self) -> None:
        """`Chair@Example.com` and `chair@example.com` are one mailbox.

        Without canonicalisation they become two pseudonyms, and the extractor reads one
        person as two — which is a graph error, arriving via a masking decision.
        """
        text = "Chair@Example.com wrote; reply to chair@example.com."
        result = mask(text)
        assert len({span.token for span in result.spans}) == 1

    def test_8_round_trip_is_the_identity_outside_spans(self) -> None:
        text = (
            f"Ring {SHORT_EMAIL} or +44 20 7946 0958, and the file is 4111 1111 1111 1111 "
            f"under 123-45-6789 for {LONG_EMAIL}."
        )
        result = mask(text)
        assert result.spans, "the fixture must actually contain PII"

        inside = masked_positions(result)
        for offset in range(len(result.masked_text) + 1):
            if offset in inside:
                continue
            assert result.to_masked(result.to_original(offset)) == offset

        inside_original = original_positions(result)
        for offset in range(len(text) + 1):
            if offset in inside_original:
                continue
            assert result.to_original(result.to_masked(offset)) == offset

    def test_9_non_ascii_offsets_are_characters_not_bytes(self) -> None:
        """Devanagari and emoji, with the byte index asserted to be different.

        A byte-indexed implementation passes every ASCII test in this file. The only way
        to catch it is to assert that the recorded offset is the *character* index and
        that the byte index it would have produced is a different number.
        """
        text = "नमस्ते 🙏 लिखें chair@example.com को धन्यवाद"
        result = mask(text)

        span = result.spans[0]
        assert text[span.orig_start : span.orig_end] == "chair@example.com"
        assert span.orig_start == text.index("chair@example.com")

        byte_index = len(text[: span.orig_start].encode("utf-8"))
        assert byte_index != span.orig_start, "fixture must distinguish the two indices"

        assert_spans_are_coherent(text, result)
        # And the surrounding non-ASCII text is untouched.
        assert "नमस्ते 🙏 लिखें " in result.masked_text
        assert result.masked_text.endswith(" को धन्यवाद")

    def test_10_overlapping_hits_resolve_to_the_longest_match(self) -> None:
        text = "0123456789ABCDEFGHIJ rest"
        detectors: tuple[PiiDetector, ...] = (
            StubDetector(PiiType.PERSON, "short", (Detection(0, 5),)),
            StubDetector(PiiType.EMAIL, "long", (Detection(3, 20),)),
        )
        result = mask(text, detectors)

        assert len(result.spans) == 1
        span = result.spans[0]
        assert (span.orig_start, span.orig_end) == (3, 20), "the longer hit must win"
        assert span.pii_type is PiiType.EMAIL
        assert_spans_are_coherent(text, result)

    def test_10b_longest_wins_even_when_the_shorter_hit_starts_first(self) -> None:
        """The greedy sweep must be by length, not by position.

        A left-to-right sweep that accepts the first hit and rejects anything touching it
        would keep the short one here. It reads as "longest match wins" until two
        detectors disagree about where an entity starts.
        """
        detectors: tuple[PiiDetector, ...] = (
            StubDetector(PiiType.PERSON, "early-short", (Detection(0, 4),)),
            StubDetector(PiiType.PHONE, "late-long", (Detection(2, 18),)),
        )
        result = mask("0123456789ABCDEFGH", detectors)
        assert [(s.orig_start, s.orig_end) for s in result.spans] == [(2, 18)]

    def test_10c_equal_length_overlap_falls_to_detector_order(self) -> None:
        """Ties need a rule, or the output depends on dict ordering somewhere."""
        first = StubDetector(PiiType.GOV_ID, "first", (Detection(0, 6),))
        second = StubDetector(PiiType.FINANCIAL, "second", (Detection(3, 9),))

        assert mask("0123456789", (first, second)).spans[0].pii_type is PiiType.GOV_ID
        assert mask("0123456789", (second, first)).spans[0].pii_type is PiiType.FINANCIAL


# --------------------------------------------------------------------------- offsets


class TestOffsetTranslation:
    def test_offsets_inside_a_token_resolve_to_the_start_of_the_run(self) -> None:
        """Ambiguous by construction, so the rule is documented and asserted.

        A token stands in for a run of a different length; there is no per-character
        correspondence. Resolving to the start keeps the function monotonic and never
        points part-way through a name.
        """
        text = f"x {LONG_EMAIL} y"
        result = mask(text)
        span = result.spans[0]

        interior = span.masked_start + 3
        assert span.masked_start < interior < span.masked_end
        assert result.to_original(interior) == span.orig_start
        assert result.to_original(span.masked_end) == span.orig_end

    def test_translation_is_monotonic(self) -> None:
        text = f"{SHORT_EMAIL} then {LONG_EMAIL} then +44 20 7946 0958"
        result = mask(text)
        values = [result.to_original(i) for i in range(len(result.masked_text) + 1)]
        assert values == sorted(values)

    def test_original_range_rounds_outward(self) -> None:
        """A slice that clips a token must still cover the whole entity.

        S5 promises never to split inside a span. This is what keeps a broken promise
        from becoming a citation that highlights half an email address.
        """
        text = f"x {LONG_EMAIL} y"
        result = mask(text)
        span = result.spans[0]

        start, end = result.original_range(span.masked_start + 2, span.masked_end - 2)
        assert (start, end) == (span.orig_start, span.orig_end)

    def test_original_range_of_a_clean_slice_is_two_translations(self) -> None:
        text = f"lead {LONG_EMAIL} tail"
        result = mask(text)
        span = result.spans[0]

        assert result.original_range(0, span.masked_start) == (0, span.orig_start)
        assert result.original_range(span.masked_end, len(result.masked_text)) == (
            span.orig_end,
            len(text),
        )

    def test_original_range_never_inverts(self) -> None:
        """Both boundaries inside one token is the case that produces start > end."""
        text = f"x {LONG_EMAIL} y"
        result = mask(text)
        span = result.spans[0]

        start, end = result.original_range(span.masked_start + 1, span.masked_start + 2)
        assert start <= end

    def test_out_of_range_offsets_are_refused(self) -> None:
        result = mask("nothing here")
        with pytest.raises(ValueError, match="outside masked_text"):
            result.to_original(len(result.masked_text) + 1)
        with pytest.raises(ValueError, match="outside masked_text"):
            result.to_original(-1)
        with pytest.raises(ValueError, match="negative"):
            result.to_masked(-1)

    def test_original_range_refuses_an_inverted_request(self) -> None:
        result = mask("nothing here")
        with pytest.raises(ValueError, match="precedes"):
            result.original_range(5, 2)


# --------------------------------------------------------------------------- tokens


class TestTokensAndVaultKeys:
    def test_masking_is_reproducible(self) -> None:
        """Chunking, embedding and extraction all key on the masked text (§4.14)."""
        text = f"{LONG_EMAIL} and 4111 1111 1111 1111 and 123-45-6789"
        first = mask(text)
        second = mask(text)

        assert first.masked_text == second.masked_text
        assert [s.model_dump() for s in first.spans] == [s.model_dump() for s in second.spans]

    def test_the_same_value_gets_different_tokens_in_different_documents(self) -> None:
        """Pseudonyms must not be a cross-document join key.

        A token that were a pure function of the value would let anyone able to read
        masked text correlate one person across every document they appear in, without
        ever holding the vault.
        """
        text = "chair@example.com signed."
        one = mask(text, namespace="document-one")
        two = mask(text, namespace="document-two")

        assert one.spans[0].token != two.spans[0].token
        assert one.spans[0].vault_key != two.spans[0].vault_key

    def test_the_namespace_makes_masking_repeatable_for_one_document(self) -> None:
        text = "chair@example.com signed."
        assert mask(text, namespace="doc").spans[0].token == (
            mask(text, namespace="doc").spans[0].token
        )

    def test_the_default_namespace_is_the_content_hash(self) -> None:
        """So `mask(text)` alone is deterministic, which the eval harness depends on."""
        text = "chair@example.com signed."
        from jutsu_core import content_hash_of

        assert (
            mask(text).spans[0].token == mask(text, namespace=content_hash_of(text)).spans[0].token
        )

    def test_tokens_are_shaped_for_a_model_to_read(self) -> None:
        result = mask(f"{LONG_EMAIL} and 123-45-6789")
        tokens = [span.token for span in result.spans]
        assert any(token.startswith("[EMAIL_") for token in tokens)
        assert any(token.startswith("[GOV_ID_") for token in tokens)
        assert all(token.endswith("]") for token in tokens)

    def test_tokens_stay_distinct_across_many_entities(self) -> None:
        """Exercises collision resolution.

        Two hundred entities over a 1024-value suffix collide with near certainty, so
        this fails outright against an implementation that derives a suffix and hopes.
        """
        addresses = [f"person{index:03d}@example.com" for index in range(200)]
        result = mask(" and ".join(addresses))

        assert len(result.spans) == len(addresses)
        assert len({span.token for span in result.spans}) == len(addresses)
        assert len({span.vault_key for span in result.spans}) == len(addresses)

    def test_a_token_never_contains_the_value_it_replaces(self) -> None:
        text = "chair@example.com and 4111 1111 1111 1111"
        result = mask(text)
        for span in result.spans:
            original = text[span.orig_start : span.orig_end]
            assert original not in span.token
            assert original not in span.vault_key

    def test_vault_keys_fit_the_column(self) -> None:
        """`pii_vault.vault_key` is `String(128)`; a longer key fails at insert time."""
        result = mask(f"{LONG_EMAIL} and 123-45-6789")
        assert all(len(span.vault_key) <= 128 for span in result.spans)


# --------------------------------------------------------------------------- detectors


class TestCanonicalisation:
    def test_canonicalisers_cover_every_type(self) -> None:
        """A type with no rule would silently split one entity into several."""
        for pii_type in PiiType:
            assert canonicalise(pii_type, "Sample Value 123") != ""

    def test_formatted_numbers_ignore_their_separators(self) -> None:
        assert canonicalise(PiiType.FINANCIAL, "4111 1111 1111 1111") == canonicalise(
            PiiType.FINANCIAL, "4111-1111-1111-1111"
        )

    def test_free_text_ignores_case_and_spacing(self) -> None:
        assert canonicalise(PiiType.PERSON, "Jane   Doe") == canonicalise(
            PiiType.PERSON, "jane doe"
        )


def detected(detector: PiiDetector, text: str) -> list[str]:
    return [text[start:end] for start, end in detector.detect(text)]


class TestEmailDetector:
    def test_finds_ordinary_addresses(self) -> None:
        assert detected(EMAIL_DETECTOR, f"to {LONG_EMAIL}.") == [LONG_EMAIL]

    def test_does_not_swallow_the_trailing_sentence_period(self) -> None:
        """A greedy TLD match takes the full stop with it and masks one character too many."""
        assert detected(EMAIL_DETECTOR, "mail chair@example.com.") == ["chair@example.com"]

    def test_ignores_a_bare_domain(self) -> None:
        assert detected(EMAIL_DETECTOR, "see example.com for details") == []


class TestPhoneDetector:
    @pytest.mark.parametrize(
        "text",
        [
            "+919876543210",
            "+44 20 7946 0958",
            "+1 555 123 4567",
            "(555) 123-4567",
            "555-123-4567",
            "1-800-555-0199",
        ],
    )
    def test_finds_real_shapes(self, text: str) -> None:
        assert detected(PHONE_DETECTOR, f"call {text} now") == [text]

    def test_a_number_glued_to_a_word_is_left_alone(self) -> None:
        """A stated limit of a pattern-only detector, not an oversight.

        The pattern refuses to start immediately after a letter or digit, because that
        is how it avoids masking the tail of an identifier — `SN123-456-7890` is a part
        number, not somebody's landline. The cost is a number written with no separator
        before it, which does not occur in the pilot corpus and is worth the precision.

        Recorded as a test rather than a comment so that widening the pattern later is a
        deliberate decision with a failing test attached, and so the gap is visible to
        anyone assessing what "masked" means here (ADR 0005).
        """
        assert detected(PHONE_DETECTOR, "ref+442079460958") == []
        assert detected(PHONE_DETECTOR, "SN555-123-4567") == []
        # A separator of any kind is enough for it to fire.
        assert detected(PHONE_DETECTOR, "ref: +442079460958") == ["+442079460958"]

    @pytest.mark.parametrize(
        "text",
        [
            "order 5551234567 shipped",  # a bare digit run is an identifier far more often
            "server 192.168.1.100 is up",
            "the 2024-2025 financial year",
            "version 1.2.3 released",
            "extension 4021 please",
        ],
    )
    def test_leaves_things_that_are_not_phone_numbers(self, text: str) -> None:
        """Every false positive is retrieval quality spent for no privacy gain."""
        assert detected(PHONE_DETECTOR, text) == []


class TestGovIdDetector:
    def test_finds_a_valid_ssn(self) -> None:
        assert detected(SSN_DETECTOR, "ssn 123-45-6789 on file") == ["123-45-6789"]

    @pytest.mark.parametrize("value", ["000-45-6789", "666-45-6789", "900-45-6789"])
    def test_rejects_areas_the_ssa_never_issues(self, value: str) -> None:
        assert detected(SSN_DETECTOR, f"ref {value} here") == []

    @pytest.mark.parametrize("value", ["123-00-6789", "123-45-0000"])
    def test_rejects_reserved_group_and_serial(self, value: str) -> None:
        assert detected(SSN_DETECTOR, f"ref {value} here") == []


class TestFinancialDetectors:
    @pytest.mark.parametrize(
        "value", ["4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111"]
    )
    def test_finds_a_card_that_passes_luhn(self, value: str) -> None:
        assert detected(CARD_DETECTOR, f"card {value} exp") == [value]

    def test_rejects_a_digit_run_that_fails_luhn(self) -> None:
        """Without the check digit this pattern masks every long reference number."""
        assert detected(CARD_DETECTOR, "reference 1234567890123 attached") == []

    @pytest.mark.parametrize("value", ["GB82 WEST 1234 5698 7654 32", "DE89370400440532013000"])
    def test_finds_an_iban_that_passes_mod_97(self, value: str) -> None:
        assert detected(IBAN_DETECTOR, f"pay {value} today") == [value]

    def test_rejects_an_iban_with_a_bad_check(self) -> None:
        assert detected(IBAN_DETECTOR, "pay GB00 WEST 1234 5698 7654 32 today") == []


class TestDetectorInteraction:
    def test_a_card_beats_a_phone_shaped_hit_on_part_of_it(self) -> None:
        """The reason `_select` is longest-first rather than first-come."""
        text = "charge 4111 1111 1111 1111 today"
        result = mask(text)

        assert len(result.spans) == 1
        assert result.spans[0].pii_type is PiiType.FINANCIAL
        assert text[result.spans[0].orig_start : result.spans[0].orig_end] == (
            "4111 1111 1111 1111"
        )

    def test_a_mixed_document_masks_every_type_and_still_reconstructs(self) -> None:
        text = (
            "Hi chair@example.com, ring +44 20 7946 0958 or 555-123-4567.\n"
            "Card 4111 1111 1111 1111, SSN 123-45-6789, IBAN DE89370400440532013000.\n"
            "Nothing else here is sensitive."
        )
        result = mask(text)

        found = {span.pii_type for span in result.spans}
        assert found == {PiiType.EMAIL, PiiType.PHONE, PiiType.FINANCIAL, PiiType.GOV_ID}
        assert_spans_are_coherent(text, result)
        assert "chair@example.com" not in result.masked_text
        assert "4111" not in result.masked_text
        assert "Nothing else here is sensitive." in result.masked_text

    def test_the_default_order_is_fixed(self) -> None:
        """Order is part of the §9.1 contract: it decides equal-length ties."""
        assert [detector.name for detector in DEFAULT_DETECTORS] == [
            "email",
            "iban",
            "payment_card",
            "us_ssn",
            "phone",
        ]


class TestMalformedDetectors:
    def test_empty_and_out_of_bounds_ranges_are_dropped(self) -> None:
        """One broken detector must not take down an ingestion run.

        A zero-width span would also put a token in the output with no original text
        behind it, which breaks reconstruction for every span after it.
        """
        text = "plain text"
        detectors: tuple[PiiDetector, ...] = (
            StubDetector(
                PiiType.PERSON,
                "broken",
                (Detection(3, 3), Detection(-1, 4), Detection(5, 999), Detection(6, 4)),
            ),
        )
        result = mask(text, detectors)
        assert result.masked_text == text
        assert result.spans == []


class TestExhaustiveCombinations:
    """Every arrangement of two entities among three fillers, checked end to end.

    A thousand-odd documents built by `itertools.product` rather than by a random
    generator: the point of a fuzz over an offset map is coverage of the awkward
    adjacencies — empty filler between two entities, an entity at index 0, an entity
    touching the end, non-ASCII either side — and a product enumerates all of them
    exactly once and reproduces byte-for-byte on every machine. A seeded RNG would
    achieve the same coverage less certainly and would need explaining every time a
    failure could not be reproduced from the seed alone.

    This is the M1 gate line "every chunk offset resolves to matching text in the
    original document body" (§21), proven at the layer that produces the offsets.
    """

    #: A quoted reply block. Built from chr(10) rather than an escape so the fixture
    #: reads in the file exactly as it exists in memory.
    QUOTED = chr(10) + "> quoted" + chr(10)

    FILLERS = ("", " sent to ", "नमस्ते 🙏 ", QUOTED)
    ENTITIES = (
        "chair@example.com",
        "+44 20 7946 0958",
        "4111 1111 1111 1111",
        "123-45-6789",
    )

    def _documents(self) -> Iterator[str]:
        for lead, first, middle, second, tail in product(
            self.FILLERS, self.ENTITIES, self.FILLERS, self.ENTITIES, self.FILLERS
        ):
            yield f"{lead}{first}{middle}{second}{tail}"

    def test_every_arrangement_reconstructs_and_round_trips(self) -> None:
        checked = 0
        for text in self._documents():
            result = mask(text)
            checked += 1

            assert reconstruct(text, result) == text, text
            assert_spans_are_coherent(text, result)

            inside = masked_positions(result)
            for offset in range(len(result.masked_text) + 1):
                if offset not in inside:
                    assert result.to_masked(result.to_original(offset)) == offset, text

            # Leakage is deliberately NOT asserted here, and the reason is worth
            # stating. This product builds arrangements the detectors decline by
            # design - a number glued to the digit before it, with no separator - so
            # "every entity is masked" is false over this space and would have to be
            # weakened until it asserted nothing. The declining is itself tested, in
            # `test_a_number_glued_to_a_word_is_left_alone`, and leakage is asserted on
            # realistic documents in `TestDetectorInteraction`. What this class is for
            # is the offset map, which must be exactly right in all 1024 arrangements.

        assert checked == len(self.FILLERS) ** 3 * len(self.ENTITIES) ** 2

    def test_original_range_tiles_the_document_without_gaps(self) -> None:
        """What S5 will do: cut the masked text, store original offsets, lose nothing.

        Boundaries are taken outside every span, as §9.2 requires the chunker to do, and
        the resulting original ranges must join end to start and cover the whole body.
        """
        for text in self._documents():
            result = mask(text)
            inside = masked_positions(result)
            boundaries = [0] + [
                offset
                for offset in range(1, len(result.masked_text))
                if offset % 7 == 0 and offset not in inside
            ]
            boundaries.append(len(result.masked_text))

            previous_end = 0
            for start, end in pairwise(boundaries):
                original_start, original_end = result.original_range(start, end)
                assert original_start == previous_end, text
                assert original_end >= original_start, text
                previous_end = original_end
            assert previous_end == len(text), text


class TestUuidsAreNotPaymentCards:
    """A UUID is an opaque internal id, and masking one is a false positive.

    Measured on the 200-document pilot: document id
    `d97dced3-342a-4093-ae99-80057186318f` produced the match `99-80057186318` - 13
    digits, Luhn-valid - and the ingestion log was reported as containing raw financial
    PII. Across 20 000 random UUIDs it happened 0.29% of the time, so a 45 000-document
    run would fail M1's "zero raw PII in captured logs" clause roughly 130 times over
    identifiers that carry nothing.

    Excluding letters at the match boundaries fixed the first id and took the rate to
    about 0.1%, because a run can still *end* on a group boundary with a hyphen on each
    side - which is how `48751163-2628-4948-a42a-c3ad1c0028eb` was found, by this test
    failing. The pattern now encodes card *grouping* instead of a free separator class.

    **The bulk sweep is seeded on purpose.** It used `uuid.uuid4()`, so at a 0.1%
    residual rate it passed roughly one run in three - and it did pass, on the run that
    was used to call the first fix verified. A regression test that reports a real defect
    only sometimes is worse than none: it teaches the reader that a red run is weather.
    The generator below draws version-4-shaped ids from a fixed seed, so the same 20 000
    identifiers are checked on every machine and a failure names one of them.
    """

    #: The exact ids from the pilot and from this test's own first failure. Kept verbatim
    #: so neither can come back unnoticed.
    PILOT_UUID = "d97dced3-342a-4093-ae99-80057186318f"
    BOUNDARY_UUID = "48751163-2628-4948-a42a-c3ad1c0028eb"

    @staticmethod
    def _uuids(count: int, seed: int = 0) -> list[str]:
        # S311: a seeded generator is the entire point - these identifiers are test
        # input, and a CSPRNG here would put the flakiness straight back.
        random_source = random.Random(seed)  # noqa: S311
        values: list[str] = []
        for _ in range(count):
            body = "".join(random_source.choice("0123456789abcdef") for _ in range(31))
            values.append(
                f"{body[:8]}-{body[8:12]}-4{body[12:15]}-"
                f"{random_source.choice('89ab')}{body[15:18]}-{body[18:30]}"
            )
        return values

    @pytest.mark.parametrize("attribute", ["PILOT_UUID", "BOUNDARY_UUID"])
    def test_a_measured_uuid_is_not_masked(self, attribute: str) -> None:
        value = getattr(self, attribute)
        line = f"document_created org=8f14e45f document={value} chunks=1"
        assert not mask(line).spans

    def test_a_log_line_shaped_like_the_pilots_is_clean(self) -> None:
        line = (
            f"document_created org=ff805d42-ac5c-4cdd-8ea9-b49873e3dbc5 "
            f"source=f2c7bfd0-dd20-41c1-b579-1013be686516 document={self.PILOT_UUID} "
            f"chunks=1 grants=2"
        )
        assert not mask(line).spans

    def test_a_large_sample_of_uuids_is_never_masked(self) -> None:
        """20 000 is the sample the 0.29% baseline was measured over."""
        flagged = [value for value in self._uuids(20_000) if mask(f"document={value}").spans]
        assert flagged == [], f"{len(flagged)} UUIDs masked, first {flagged[:1]}"

    def test_uuid4_itself_is_never_masked(self) -> None:
        """The seeded generator imitates `uuid4`; this checks the real one agrees."""
        flagged = [
            str(value)
            for value in (uuid.uuid4() for _ in range(20_000))
            if mask(f"document={value}").spans
        ]
        assert flagged == [], f"{len(flagged)} UUIDs masked, first {flagged[:1]}"


class TestRealCardsAreStillDetected:
    """The boundary fix must not buy quiet logs by going blind."""

    @pytest.mark.parametrize(
        "text",
        [
            "card 4111 1111 1111 1111 on file",
            "card 4111-1111-1111-1111 on file",
            "card 4111111111111111 on file",
            "4111111111111111",
            "Please charge 5500 0000 0000 0004 today",
            "(4111-1111-1111-1111)",
        ],
    )
    def test_a_payment_card_is_masked(self, text: str) -> None:
        result = mask(text)
        assert result.spans, f"a real card went undetected in {text!r}"
        assert any(span.pii_type is PiiType.FINANCIAL for span in result.spans)

    def test_a_luhn_valid_run_in_prose_is_still_masked(self) -> None:
        """Delimited by spaces, so the boundary guard does not apply."""
        assert mask("reference 4111111111111111 follows").spans

    def test_other_detectors_are_untouched(self) -> None:
        result = mask("mail ada@example.com or call +1 415 555 0132")
        kinds = {span.pii_type for span in result.spans}
        assert PiiType.EMAIL in kinds
