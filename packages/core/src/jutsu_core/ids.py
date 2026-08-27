"""JUTSU IDs — the human-facing identifier.

Format: ``JUTSU-{EMP|ADM|HR}-{8 characters}``.

Four separate concepts must never be conflated, and this module owns only the last:

=========================  ====================================================
``users.id``               Database primary key. Internal, never displayed.
``users.external_id``      The primary IdP subject, where one exists. **No longer
                           the ACL principal** — migration 0008 moved that to
                           ``source_identities`` because one column cannot hold
                           six provider identities (ADR 0010). Nothing in the
                           authorisation path reads it.
``source_identities``      The **ACL principal**, one row per provider:
                           ``document_acl.principal_id`` matches
                           ``{source_system}:{subject}``, where ``subject`` is the
                           provider-native immutable id. Email is display data,
                           not an authorisation key.
``jutsu_id``               What a person reads out over the phone. Display and
                           lookup only; never an authorisation input.
=========================  ====================================================

Writing a JUTSU ID into any of the first three would silently breach the product
invariant: a grant would match a JUTSU-shaped principal and the holder would gain
visibility nobody granted them.

**Why Crockford base32 rather than "A-Z0-9 minus the confusable ones".**

The alphabet excludes ``I`` and ``L`` (confusable with ``1`` and each other),
``O`` (confusable with ``0``), and ``U`` (so a random draw cannot spell something
unfortunate). That leaves exactly 32 symbols, and 32 is the number that matters:
eight characters over 32 symbols is exactly 40 bits, so five CSPRNG bytes map onto
an id with **zero modulo bias**. A 31-symbol alphabet gives 39.634 bits and forces
either biased modulo arithmetic or a rejection loop. Crockford also publishes a
decode map, so a mis-transcribed ``O`` or ``l`` self-heals on input rather than
becoming a support ticket.

**Collision handling is a retry, not an exception.** Derived from the alphabet:
the space is 32**8 = 1,099,511,627,776. At 10**8 allocated ids the per-insert
collision rate is k/N = 9.09e-5, so five attempts fail together with probability
6.22e-21. The ledger uses ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` so a
taken id yields no row instead of raising — an ``IntegrityError`` inside the
registration transaction would abort every statement after it.
"""

from __future__ import annotations

import re
import secrets
from enum import StrEnum

__all__ = [
    "ALPHABET",
    "ID_PATTERN",
    "SUFFIX_LENGTH",
    "JutsuIdKind",
    "generate_jutsu_id",
    "is_valid_jutsu_id",
    "normalise_jutsu_id",
]

#: Crockford base32. Order is significant — it is the decode order too.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

SUFFIX_LENGTH = 8

#: Bits consumed per character. 32 symbols = 5 bits, and 8 x 5 = 40 = 5 whole bytes.
_BITS_PER_CHAR = 5

#: Crockford's published normalisation. Applied to the suffix only: the literal
#: "JUTSU-" prefix contains a U, which is not part of the encoding alphabet.
_DECODE_MAP = str.maketrans({"I": "1", "L": "1", "O": "0"})


class JutsuIdKind(StrEnum):
    """What the holder was when the id was allocated.

    Frozen at allocation rather than tracking the current role. An id is a permanent
    public reference, so a prefix that changed would silently re-point every human
    reference to it — and a person who is both HR and an employee would otherwise have
    an id that flickers. The consequence, accepted deliberately: the prefix discloses
    onboarding type to anyone who sees the id, so it must never be an authorisation
    input.
    """

    EMPLOYEE = "EMP"
    ADMIN = "ADM"
    HR = "HR"


ID_PATTERN = re.compile(rf"^JUTSU-(EMP|ADM|HR)-[{ALPHABET}]{{{SUFFIX_LENGTH}}}$")


def generate_jutsu_id(kind: JutsuIdKind) -> str:
    """One cryptographically random candidate. Uniqueness is the ledger's job.

    Reads exactly 40 bits and consumes them 5 at a time, so every id in the space is
    equally likely. Deliberately not ``secrets.choice`` in a loop — that is correct here
    too, but this makes the "5 bytes is 8 characters" property visible at the call site,
    which is the whole argument for the alphabet size.
    """
    value = int.from_bytes(secrets.token_bytes(SUFFIX_LENGTH * _BITS_PER_CHAR // 8), "big")
    suffix = "".join(
        ALPHABET[(value >> (_BITS_PER_CHAR * (SUFFIX_LENGTH - 1 - i))) & 0x1F]
        for i in range(SUFFIX_LENGTH)
    )
    return f"JUTSU-{kind.value}-{suffix}"


def normalise_jutsu_id(raw: str) -> str:
    """Best-effort repair of a hand-typed id.

    Upper-cases, drops surrounding whitespace, and applies Crockford's decode map to the
    suffix so ``O`` becomes ``0`` and ``I``/``l`` become ``1``. Returns the input
    upper-cased and otherwise untouched when it does not have the expected shape — this
    is a normaliser, not a validator, and callers must still check the result.
    """
    candidate = raw.strip().upper().replace(" ", "")
    parts = candidate.split("-")
    if len(parts) != 3:
        return candidate
    prefix, kind, suffix = parts
    return f"{prefix}-{kind}-{suffix.translate(_DECODE_MAP)}"


def is_valid_jutsu_id(candidate: str) -> bool:
    """Shape only.

    A well-formed id says nothing about whether it was ever allocated — that requires
    the ledger. Kept separate so a malformed id can be rejected without a database
    round trip, while a well-formed unknown one still takes the same path as a real
    one and cannot be distinguished by timing.
    """
    return ID_PATTERN.fullmatch(candidate) is not None
