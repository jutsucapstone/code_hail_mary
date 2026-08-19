"""JUTSU ID generation, normalisation and validation.

The distribution test matters more than it looks. The alphabet was chosen so that eight
characters is exactly 40 bits and five random bytes map on with no modulo bias; an
implementation that used `% 32` on a larger integer, or a 31-symbol alphabet, would still
produce ids that pass every other test here while quietly favouring the first few
symbols. `test_every_symbol_is_reachable` is what would catch that.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest
from jutsu_core.ids import (
    ALPHABET,
    SUFFIX_LENGTH,
    JutsuIdKind,
    generate_jutsu_id,
    is_valid_jutsu_id,
    normalise_jutsu_id,
)


class TestAlphabet:
    def test_is_crockford_base32(self) -> None:
        assert len(ALPHABET) == 32
        assert len(set(ALPHABET)) == 32, "a repeated symbol would silently shrink the space"

    @pytest.mark.parametrize("excluded", ["I", "L", "O", "U"])
    def test_confusable_and_unfortunate_symbols_are_absent(self, excluded: str) -> None:
        """I/L against 1, O against 0, and U so a draw cannot spell something unfortunate."""
        assert excluded not in ALPHABET

    def test_eight_characters_is_a_whole_number_of_bytes(self) -> None:
        """The property the whole alphabet choice rests on.

        32**8 == 256**5, so five CSPRNG bytes map onto eight characters exactly. If this
        ever stops holding, generation acquires modulo bias and the alphabet needs
        rejection sampling instead.
        """
        assert len(ALPHABET) ** SUFFIX_LENGTH == 256**5


class TestGeneration:
    @pytest.mark.parametrize("kind", list(JutsuIdKind))
    def test_generated_ids_are_well_formed(self, kind: JutsuIdKind) -> None:
        for _ in range(50):
            generated = generate_jutsu_id(kind)
            assert is_valid_jutsu_id(generated)
            assert generated.startswith(f"JUTSU-{kind.value}-")

    def test_ids_are_not_repeated(self) -> None:
        """A weak source would show up here long before it showed up in production."""
        produced = {generate_jutsu_id(JutsuIdKind.EMPLOYEE) for _ in range(2000)}
        assert len(produced) == 2000

    def test_every_symbol_is_reachable(self) -> None:
        """No symbol may be unreachable, and none may dominate.

        Catches a biased mapping — `% 32` over a wider integer, or an off-by-one on the
        shift — which would leave part of the alphabet unused and shrink the real space
        without any other test noticing.
        """
        counts: Counter[str] = Counter()
        for _ in range(4000):
            counts.update(generate_jutsu_id(JutsuIdKind.EMPLOYEE).split("-")[2])

        assert set(counts) == set(ALPHABET), "some symbols are never produced"

        total = sum(counts.values())
        expected = total / len(ALPHABET)
        # Generous bound: this is a smoke test for bias, not a statistical proof, and it
        # must not flake. A real bias shows up as an absent symbol or a 2x skew.
        assert max(counts.values()) < expected * 1.6
        assert min(counts.values()) > expected * 0.4


class TestNormalisation:
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            ("jutsu-emp-4p9k2mzr", "JUTSU-EMP-4P9K2MZR"),
            ("  JUTSU-EMP-4P9K2MZR  ", "JUTSU-EMP-4P9K2MZR"),
            ("JUTSU-EMP-4P9K2MZ R", "JUTSU-EMP-4P9K2MZR"),
            # Crockford's decode map: the reason those symbols were excluded.
            ("JUTSU-EMP-4P9KIMZR", "JUTSU-EMP-4P9K1MZR"),
            ("JUTSU-EMP-4P9KLMZR", "JUTSU-EMP-4P9K1MZR"),
            ("JUTSU-EMP-4P9KOMZR", "JUTSU-EMP-4P9K0MZR"),
        ],
    )
    def test_repairs_a_hand_typed_id(self, typed: str, expected: str) -> None:
        assert normalise_jutsu_id(typed) == expected

    def test_the_prefix_is_never_decoded(self) -> None:
        """`JUTSU` contains a U, which is not in the alphabet.

        Applying the decode map to the whole string rather than the suffix would corrupt
        every id — and only for the prefix, so it would look like a mysterious lookup
        failure rather than an obvious bug.
        """
        assert normalise_jutsu_id("JUTSU-EMP-4P9K2MZR").startswith("JUTSU-")

    def test_a_shapeless_value_is_returned_upper_cased_not_mangled(self) -> None:
        """Normalisation is not validation — callers still have to check."""
        assert normalise_jutsu_id("not an id") == "NOTANID"
        assert not is_valid_jutsu_id(normalise_jutsu_id("not an id"))


class TestValidation:
    @pytest.mark.parametrize(
        "candidate",
        [
            "JUTSU-EMP-4P9K2MZR",
            "JUTSU-ADM-0123456789"[:18],
            "JUTSU-HR-ZZZZZZZZ",
        ],
    )
    def test_accepts_well_formed_ids(self, candidate: str) -> None:
        assert is_valid_jutsu_id(candidate)

    @pytest.mark.parametrize(
        "candidate",
        [
            "JUTSU-EMP-4P9K2MZ",  # too short
            "JUTSU-EMP-4P9K2MZRR",  # too long
            "JUTSU-XXX-4P9K2MZR",  # unknown kind
            "JUTSU-EMP-4P9K2MZI",  # excluded symbol
            "JUTSU-EMP-4P9K2MZU",  # excluded symbol
            "jutsu-emp-4p9k2mzr",  # not normalised
            "4P9K2MZR",  # bare suffix
            "",
        ],
    )
    def test_rejects_malformed_ids(self, candidate: str) -> None:
        assert not is_valid_jutsu_id(candidate)

    def test_pattern_is_anchored(self) -> None:
        """An unanchored pattern would accept an id with anything appended to it."""
        assert not is_valid_jutsu_id("JUTSU-EMP-4P9K2MZR; DROP TABLE users")
        assert not is_valid_jutsu_id("prefix JUTSU-EMP-4P9K2MZR")

    def test_matches_the_database_check_constraint(self) -> None:
        """The regex here and the CHECK in migration 0002 must agree.

        They are written independently — Python and Postgres — so a change to one that
        is not mirrored means ids the application generates are rejected on insert, or
        worse, ids the database accepts that the application will not recognise.
        """
        migration_pattern = re.compile(r"^JUTSU-(EMP|ADM|HR)-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{8}$")
        for kind in JutsuIdKind:
            for _ in range(20):
                generated = generate_jutsu_id(kind)
                assert migration_pattern.fullmatch(generated), generated
