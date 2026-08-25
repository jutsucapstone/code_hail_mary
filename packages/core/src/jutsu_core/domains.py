"""Canonical forms for email addresses and organisation domains.

Registration turns on one equality: the domain of a verified work address must equal the
domain the organisation claims. That comparison is a security control, so it cannot be
`a.lower() == b.lower()` — `Acme.COM`, `acme.com.`, `ACME.com` and `xn--80ak6aa92e.com`
are the same authority, and treating any of them as distinct is either a bypass or a
false rejection.

**Subdomains do not satisfy a parent, and a parent does not satisfy a subdomain.**
`alice@mail.acme.com` does not prove authority over `acme.com`: anyone who can be issued
a mailbox on any subdomain could otherwise claim the apex, and on shared hosting that is
a very low bar. Exact equality of canonical forms is the only rule, which is also the
only rule that is obvious to the person filling the form in.

Nothing here touches the network. DNS-based proof of a domain is a stronger control and a
different slice; this module is only about comparing two strings the caller supplied.
"""

from __future__ import annotations

__all__ = [
    "DomainError",
    "canonical_domain",
    "domain_of",
    "domains_match",
    "normalise_email",
]


class DomainError(ValueError):
    """A value could not be reduced to a canonical domain."""


def canonical_domain(value: str) -> str:
    """Reduce a domain to the one form everything else compares against.

    Lowercased, stripped of surrounding whitespace and of the trailing dot that makes a
    name fully qualified, and encoded to its ASCII A-label so a Unicode domain and its
    punycode spelling collapse together. `Ünicode.example` and `xn--nicode-2ya.example`
    are one authority and must not be two organisations.
    """
    cleaned = value.strip().rstrip(".").strip()
    if not cleaned:
        raise DomainError("A domain is required.")

    # A pasted address reduces to its domain rather than being rejected.
    #
    # Without this the whole address survived: the label check only requires two or more
    # non-empty dot-separated parts, and `er.ritikraj27@gmail.com` has three, so it was
    # accepted as a domain in its own right. The organisation was then created claiming
    # a "domain" containing an @, and the form told the registrant their work email had
    # to be at `er.ritikraj27@gmail.com` — advice that cannot be followed.
    #
    # `rpartition`, so an address whose local part itself contains an @ still yields the
    # real domain. `domain_of` splits before calling this, so it never reaches here with
    # one and is unaffected.
    if "@" in cleaned:
        cleaned = cleaned.rpartition("@")[2].strip()
        if not cleaned:
            raise DomainError("That does not look like a domain.")

    # `idna` is not a dependency and the stdlib codec is close enough for a comparison
    # key: it is only ever used to make two strings agree, never to resolve anything.
    # A domain that cannot be encoded is rejected rather than passed through, because
    # silently comparing raw Unicode would make the check spellable-around.
    try:
        cleaned = cleaned.encode("idna").decode("ascii")
    except UnicodeError:
        # Pure-ASCII names take this path only when a label is empty or oversized, both
        # of which are malformed. Anything already ASCII and well-formed encodes cleanly.
        raise DomainError("That does not look like a domain.") from None

    lowered = cleaned.lower()

    # One dot minimum, no empty labels: rejects "localhost", "acme." and "a..b" without
    # pretending to be a full RFC 1035 validator. The authoritative check on whether the
    # domain exists is that someone received mail at it.
    labels = lowered.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        raise DomainError("That does not look like a domain.")

    return lowered


def normalise_email(value: str) -> str:
    """Lowercase an address and canonicalise its domain, leaving the local part alone.

    The domain is case-insensitive by specification; the local part is *not*, so it is
    only stripped of surrounding whitespace. Lowercasing it would merge two addresses
    that a strict mail server treats as different people — rare, but the failure is
    handing one person another's account.

    Plus-addressing is deliberately preserved for the same reason: `ada+jutsu@acme.com`
    is a real, deliverable address and the person chose it. Stripping the tag is a
    de-duplication policy, not a normalisation, and this function is used to decide who
    someone is.
    """
    address = value.strip()
    local, separator, domain = address.rpartition("@")
    if not separator or not local:
        raise DomainError("That does not look like an email address.")
    return f"{local}@{canonical_domain(domain)}"


def domain_of(email: str) -> str:
    """The canonical domain of an address."""
    _, separator, domain = email.strip().rpartition("@")
    if not separator:
        raise DomainError("That does not look like an email address.")
    return canonical_domain(domain)


def domains_match(email: str, claimed_domain: str) -> bool:
    """Whether a verified address proves authority over a claimed organisation domain.

    Returns False rather than raising on malformed input: a caller deciding "may this
    registration proceed" wants one answer, and a malformed claim is a refusal, not a
    different kind of error. Callers that need to explain *why* validate first.
    """
    try:
        return domain_of(email) == canonical_domain(claimed_domain)
    except DomainError:
        return False
