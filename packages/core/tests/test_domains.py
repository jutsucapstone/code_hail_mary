"""The equality registration turns on.

`domains_match` decides whether a verified mailbox proves authority over a claimed
organisation domain. Every case below is a way that comparison has been got wrong in a
real product: case, a trailing dot, punycode, a subdomain treated as its parent.

These run without a database on purpose. The comparison is the security control, and a
control that can only be exercised when Postgres happens to be up is a control nobody
runs.
"""

from __future__ import annotations

import pytest
from jutsu_core.domains import (
    DomainError,
    canonical_domain,
    domain_of,
    domains_match,
    normalise_email,
)


class TestCanonicalDomain:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("example.com", "example.com"),
            ("EXAMPLE.COM", "example.com"),
            ("  Example.Com  ", "example.com"),
            # A fully-qualified name with the root label spelled out is the same authority.
            ("example.com.", "example.com"),
            ("mail.example.co.uk", "mail.example.co.uk"),
        ],
    )
    def test_reduces_to_one_form(self, raw: str, expected: str) -> None:
        assert canonical_domain(raw) == expected

    def test_unicode_and_punycode_collapse_together(self) -> None:
        """Otherwise one authority could be registered twice, spelled two ways."""
        assert canonical_domain("Bücher.example") == canonical_domain("xn--bcher-kva.example")

    @pytest.mark.parametrize("raw", ["", "   ", "localhost", "example.", ".com", "a..b"])
    def test_rejects_what_is_not_a_domain(self, raw: str) -> None:
        with pytest.raises(DomainError):
            canonical_domain(raw)


class TestNormaliseEmail:
    def test_lowercases_the_domain_but_not_the_local_part(self) -> None:
        """The domain is case-insensitive by specification; the local part is not.

        Folding the local part would merge two addresses a strict server treats as two
        people, and the failure mode is handing one of them the other's account.
        """
        assert normalise_email("Ada.Lovelace@Example.COM") == "Ada.Lovelace@example.com"

    def test_preserves_plus_addressing(self) -> None:
        """`ada+jutsu@` is a real deliverable address the person chose.

        Stripping the tag is a de-duplication policy, not a normalisation, and this
        function is used to decide who someone is.
        """
        assert normalise_email("ada+jutsu@example.com") == "ada+jutsu@example.com"

    @pytest.mark.parametrize("raw", ["ada", "@example.com", "ada@", ""])
    def test_rejects_what_is_not_an_address(self, raw: str) -> None:
        with pytest.raises(DomainError):
            normalise_email(raw)

    def test_takes_the_last_at_sign(self) -> None:
        """A quoted local part may contain `@`; the domain is what follows the last one."""
        assert domain_of('"weird@local"@example.com') == "example.com"


class TestDomainsMatch:
    def test_an_exact_match_passes(self) -> None:
        assert domains_match("ada@example.com", "example.com")

    def test_case_and_trailing_dots_do_not_defeat_it(self) -> None:
        assert domains_match("Ada@EXAMPLE.com", "example.com.")

    def test_a_different_domain_fails(self) -> None:
        """The squat this whole redesign exists to stop."""
        assert not domains_match("eve@evil.example", "microsoft.com")

    def test_a_subdomain_does_not_prove_the_parent(self) -> None:
        """Anyone issued a mailbox on any subdomain would otherwise own the apex.

        On shared or delegated hosting that is a very low bar, so exact equality is the
        rule — and it is also the only rule that is obvious to the person filling in the
        form.
        """
        assert not domains_match("ada@mail.example.com", "example.com")

    def test_the_parent_does_not_prove_a_subdomain_either(self) -> None:
        assert not domains_match("ada@example.com", "mail.example.com")

    def test_a_lookalike_suffix_does_not_match(self) -> None:
        """`notexample.com` ends with `example.com` as a string and is a different owner.

        A naive `endswith` check is the classic way this is got wrong.
        """
        assert not domains_match("ada@notexample.com", "example.com")

    def test_malformed_input_is_a_refusal_not_an_exception(self) -> None:
        """A caller asking "may this proceed" wants one answer, and this is a no."""
        assert not domains_match("not-an-address", "example.com")
        assert not domains_match("ada@example.com", "")
