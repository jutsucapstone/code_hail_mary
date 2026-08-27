"""PII masking with an offset map (spec §9.1).

The hardest correctness problem in the ingestion path, and the reason is a single
sentence: **masking changes string length, the LLM may only ever see the masked text, and
every citation must highlight a span in the original.** Three requirements that pull in
different directions, held together by `MaskResult` — this module produces one, and
`MaskResult.to_original` is the only sanctioned way to travel between the two coordinate
systems.

What this module guarantees:

* **Determinism.** The same text and the same detectors produce a byte-identical
  `masked_text` and identical tokens, on every machine and every run. Chunking, embedding
  and extraction all key on the masked text, so a masking pass that varied would make the
  pipeline non-idempotent (§4.14).
* **Character offsets, never byte offsets.** The corpus contains Devanagari and emoji, and
  a byte index mis-highlights every citation in a document containing either. Nothing here
  calls `encode()` on a span boundary.
* **Non-overlapping spans, longest match wins.** Detectors disagree; the merge is a
  deterministic interval selection rather than whichever detector happened to run last.
* **Tokens are pseudonyms, not identifiers.** `[EMAIL_A7]` is derived from an HMAC-shaped
  digest over the entity *and the document*, so the same address is one token within a
  document and a different one in the next. That keeps extraction able to co-refer inside
  a document without making the masked corpus a cross-document correlation table.

**Not implemented here: PERSON and ADDRESS.** Both need named-entity recognition, which
means a model, which is a stack decision rather than a slice — see ADR 0005. `PiiType`
carries them and `mask` will handle them the moment a detector for them is passed in;
what would be wrong is a regex that pretends to find names and quietly misses most of
them, because every downstream measurement would then be computed against a corpus
believed to be masked (§4.11).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Final, NamedTuple, Protocol, runtime_checkable

from jutsu_core.ids import ALPHABET
from jutsu_core.models import MaskedSpan, MaskResult, PiiType, content_hash_of

__all__ = [
    "CARD_DETECTOR",
    "DEFAULT_DETECTORS",
    "EMAIL_DETECTOR",
    "IBAN_DETECTOR",
    "PHONE_DETECTOR",
    "SSN_DETECTOR",
    "Detection",
    "PiiDetector",
    "RegexDetector",
    "canonicalise",
    "mask",
]


class Detection(NamedTuple):
    """One character range a detector claims. Half-open, like every offset in JUTSU."""

    start: int
    end: int


@runtime_checkable
class PiiDetector(Protocol):
    """One kind of sensitive value, found in text.

    Detectors know nothing about masking, tokens or the vault — they answer "where" and
    nothing else, so a new source of detections (a model, a customer-supplied dictionary,
    an allow-list of internal identifiers) is a new implementation of three members and
    no change here.

    A detector must be **pure and deterministic**: same text in, same ranges out, in the
    same order. `mask` sorts what it receives, so ordering is not load-bearing for
    correctness, but a detector that returned different ranges on a second call would
    make the whole pipeline non-reproducible.
    """

    # Read-only, and that is not decoration. A protocol declaring a plain mutable
    # attribute is invariant in it, so a frozen dataclass — which is what every detector
    # here is, because detectors are constants — would not satisfy it. Properties state
    # the truth and accept both shapes.
    @property
    def pii_type(self) -> PiiType: ...

    @property
    def name(self) -> str:
        """Stable identifier, for provenance and for naming a detector in a failure."""
        ...

    def detect(self, text: str) -> Iterable[Detection]: ...


# --------------------------------------------------------------------------- canonical


def _compact_casefold(matched: str) -> str:
    """Collapse internal whitespace and case. The rule for free text."""
    return " ".join(matched.split()).casefold()


def _alnum_upper(matched: str) -> str:
    """Keep letters and digits only, upper-cased. The rule for formatted numbers.

    Handles cards, IBANs, phone numbers and government ids together: separators are
    presentation, and an IBAN's letters are part of the value while a phone's `+` is not.
    """
    return "".join(character for character in matched if character.isalnum()).upper()


#: How a matched string is reduced before it is used as an entity's identity.
#:
#: Two renderings of one value must collapse, or the same card gets two pseudonyms in one
#: document and the extractor reads them as two cards. Keyed on the type rather than the
#: detector because the rule is a property of the data: `4111 1111 1111 1111` and
#: `4111-1111-1111-1111` are one number, `Bob@Example.com` and `bob@example.com` are one
#: mailbox, and no detector should be able to disagree.
#:
#: Exhaustive over `PiiType`, and `test_canonicalisers_cover_every_type` keeps it so — a
#: type added without an entry would otherwise fall back to identity and silently split
#: one entity into several.
_CANONICALISERS: Final[dict[PiiType, Callable[[str], str]]] = {
    PiiType.PERSON: _compact_casefold,
    PiiType.EMAIL: _compact_casefold,
    PiiType.ADDRESS: _compact_casefold,
    PiiType.PHONE: _alnum_upper,
    PiiType.GOV_ID: _alnum_upper,
    PiiType.FINANCIAL: _alnum_upper,
}


def canonicalise(pii_type: PiiType, matched: str) -> str:
    """The identity of a matched value, for token and vault-key derivation.

    A direct lookup with no fallback, for the same reason `role_label` has none: a type
    without a rule must fail loudly at the first document rather than quietly give every
    formatting variant its own pseudonym.
    """
    return _CANONICALISERS[pii_type](matched)


# --------------------------------------------------------------------------- validators


def _luhn_ok(digits: str) -> bool:
    """The check digit every payment card carries.

    Without it the card pattern matches any thirteen-to-nineteen digit run — order
    numbers, message ids, the numeric part of a URL — and masking those costs retrieval
    quality for no privacy gain.
    """
    total = 0
    for index, character in enumerate(reversed(digits)):
        value = int(character)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_ok(matched: str) -> bool:
    digits = _alnum_upper(matched)
    return len(digits) in range(13, 20) and digits.isdigit() and _luhn_ok(digits)


def _iban_ok(matched: str) -> bool:
    """ISO 13616 mod-97: move the first four characters to the end, letters as 10-35."""
    compact = _alnum_upper(matched)
    if not 15 <= len(compact) <= 34:
        return False

    remainder = 0
    for character in compact[4:] + compact[:4]:
        if character.isdigit():
            remainder = (remainder * 10 + int(character)) % 97
        elif "A" <= character <= "Z":
            remainder = (remainder * 100 + (ord(character) - 55)) % 97
        else:
            return False
    return remainder == 1


#: A phone number has no checksum, so the only available check is how many digits it has.
#: ITU E.164 caps a subscriber number at fifteen; below seven it is an extension or a
#: year, not a number anyone can dial.
_PHONE_DIGITS = range(7, 16)


def _phone_ok(matched: str) -> bool:
    return len(_alnum_upper(matched)) in _PHONE_DIGITS


# --------------------------------------------------------------------------- detectors


@dataclass(frozen=True, slots=True)
class RegexDetector:
    """A detector backed by a pattern and an optional check on what it matched.

    **Patterns must express context with look-around, never by consuming it.** A pattern
    that swallows the space before a number masks the space too, which shifts every
    following offset by one and puts a stray character inside the vault.
    """

    pii_type: PiiType
    name: str
    pattern: re.Pattern[str]
    #: Rejects a syntactic match that is not really an instance — a Luhn failure, a digit
    #: count outside E.164. `None` where the pattern alone is the whole rule.
    validate: Callable[[str], bool] | None = None

    def detect(self, text: str) -> Iterator[Detection]:
        for match in self.pattern.finditer(text):
            if self.validate is None or self.validate(match.group()):
                yield Detection(match.start(), match.end())


EMAIL_DETECTOR: Final = RegexDetector(
    pii_type=PiiType.EMAIL,
    name="email",
    pattern=re.compile(
        r"[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
    ),
)

#: US Social Security number, with the ranges the SSA never issues excluded in the
#: pattern itself: area 000, 666 and 900-999, group 00, serial 0000. Those exclusions are
#: the whole precision of this detector — `NNN-NN-NNNN` alone also matches part numbers.
SSN_DETECTOR: Final = RegexDetector(
    pii_type=PiiType.GOV_ID,
    name="us_ssn",
    pattern=re.compile(
        r"(?<![0-9\-])(?!000|666|9[0-9]{2})[0-9]{3}-(?!00)[0-9]{2}-(?!0000)[0-9]{4}(?![0-9\-])"
    ),
)

CARD_DETECTOR: Final = RegexDetector(
    pii_type=PiiType.FINANCIAL,
    name="payment_card",
    pattern=re.compile(r"(?<![0-9])[0-9](?:[ \-]?[0-9]){12,18}(?![0-9])"),
    validate=_card_ok,
)

IBAN_DETECTOR: Final = RegexDetector(
    pii_type=PiiType.FINANCIAL,
    name="iban",
    pattern=re.compile(
        r"(?<![0-9A-Za-z])[A-Z]{2}[0-9]{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?"
        r"(?![0-9A-Za-z])"
    ),
    validate=_iban_ok,
)

#: The least precise detector in the set, and deliberately the last one in
#: `DEFAULT_DETECTORS` so that an equally long match from a checksummed detector wins the
#: tie. Three shapes: compact E.164, a grouped international number, and the North
#: American form. A bare run of digits with no `+` and no separators is *not* matched —
#: it is far more often an identifier than a phone number, and masking identifiers
#: shreds retrieval.
PHONE_DETECTOR: Final = RegexDetector(
    pii_type=PiiType.PHONE,
    name="phone",
    pattern=re.compile(
        r"""
        (?<![0-9A-Za-z])
        (?:
            \+ [0-9]{7,15}
          | \+ [0-9]{1,3} [\s.\-]? (?: \( [0-9]{1,4} \) [\s.\-]? )?
                [0-9]{2,4} (?: [\s.\-] [0-9]{2,4} ){1,4}
          | (?: \+? 1 [\s.\-] )? (?: \( [0-9]{3} \) \s? | [0-9]{3} [\s.\-] )
                [0-9]{3} [\s.\-] [0-9]{4}
        )
        (?![0-9])
        """,
        re.VERBOSE,
    ),
    validate=_phone_ok,
)

#: The order is fixed and it is part of the contract (§9.1). It decides nothing about
#: which text is masked — that is settled by longest-match — and everything about which
#: detector wins when two claim ranges of the *same* length. Checksummed detectors
#: therefore come before the pattern-only one.
DEFAULT_DETECTORS: Final[tuple[PiiDetector, ...]] = (
    EMAIL_DETECTOR,
    IBAN_DETECTOR,
    CARD_DETECTOR,
    SSN_DETECTOR,
    PHONE_DETECTOR,
)


# --------------------------------------------------------------------------- masking


class _Hit(NamedTuple):
    start: int
    end: int
    pii_type: PiiType
    #: Position of the detector in the sequence passed to `mask`. The tie-break.
    order: int


def _select(text: str, detectors: Sequence[PiiDetector]) -> list[_Hit]:
    """Every detection, reduced to a sorted, non-overlapping set. Longest match wins.

    Greedy interval selection over candidates sorted longest-first: take a hit if it
    overlaps nothing already taken. That is what makes "longest match wins" hold
    *globally* rather than only against whatever came earlier in the text — a nineteen
    character card number written as `4111 1111 1111 1111` beats a phone-shaped hit on
    part of it, whichever of the two starts first.

    Ties are broken by the detector's position in the sequence, then by start, then by
    type name. Detector order outranks position deliberately: it is the whole meaning of
    the fixed order in `DEFAULT_DETECTORS`, which puts the checksummed detectors first so
    that a card beats a phone-shaped hit of the same length wherever the two start. The
    key is total, so the result cannot depend on iteration order anywhere.
    """
    candidates: list[_Hit] = []
    for order, detector in enumerate(detectors):
        for start, end in detector.detect(text):
            # A detector returning an empty or out-of-bounds range is a bug in that
            # detector, and a zero-width span would put a token in the output with no
            # original text behind it. Dropped rather than raised: one malformed detector
            # must not take down an ingestion run.
            if 0 <= start < end <= len(text):
                candidates.append(_Hit(start, end, detector.pii_type, order))

    candidates.sort(key=lambda hit: (hit.start - hit.end, hit.order, hit.start, hit.pii_type.value))

    accepted: list[_Hit] = []
    starts: list[int] = []
    for hit in candidates:
        index = bisect_right(starts, hit.start)
        if index and accepted[index - 1].end > hit.start:
            continue
        if index < len(accepted) and accepted[index].start < hit.end:
            continue
        accepted.insert(index, hit)
        starts.insert(index, hit.start)
    return accepted


#: Two Crockford base32 characters, giving 1024 pseudonyms per type per document. Short
#: because the token sits inline in text a model reads, and every character of it is
#: context spent on nothing. Collisions are expected at that width — thirty entities in
#: 1024 buckets collide about a third of the time — so `_allocate_token` resolves them
#: rather than treating them as improbable.
_TOKEN_SUFFIX_LENGTH: Final = 2

#: Widen the suffix after this many colliding attempts. A document with more than a
#: thousand distinct entities of one type is pathological, but it must produce correct
#: output rather than a hang or an exception inside an ingestion job.
_WIDEN_AFTER: Final = 8


def _digest(*parts: str) -> bytes:
    """Domain-separated digest. NUL joins, because it cannot occur in any part."""
    return sha256("\x00".join(parts).encode("utf-8")).digest()


def _allocate_token(namespace: str, pii_type: PiiType, identity: str, taken: set[str]) -> str:
    """A pseudonym for one entity, stable within a document and unique within it.

    Derived from the document namespace as well as the value, so the same address in two
    documents gets two tokens. Deliberate: a token that were a pure function of the value
    would turn the masked corpus into a join key, and anyone able to read masked text
    could correlate an individual across every document they appear in without ever
    holding the vault.
    """
    suffix_length = _TOKEN_SUFFIX_LENGTH
    attempt = 0
    while True:
        digest = _digest(namespace, "token", pii_type.value, identity, str(attempt))
        suffix = "".join(ALPHABET[byte & 0x1F] for byte in digest[:suffix_length])
        token = f"[{pii_type.name}_{suffix}]"
        if token not in taken:
            return token
        attempt += 1
        if attempt % _WIDEN_AFTER == 0:
            suffix_length += 1


def _vault_key(namespace: str, pii_type: PiiType, identity: str) -> str:
    """Where the original value is filed in `pii_vault`.

    Namespaced by document, because `pii_vault.vault_key` is the primary key on its own
    and the same address genuinely appears in thousands of documents. Derived rather than
    random so that re-masking the same document yields the same keys and a re-ingest
    updates rows instead of orphaning them.
    """
    return _digest(namespace, "vault", pii_type.value, identity).hex()


def mask(
    text: str,
    detectors: Sequence[PiiDetector] = DEFAULT_DETECTORS,
    *,
    namespace: str | None = None,
) -> MaskResult:
    """Replace every detected value with a stable token, and record the offset map.

    `namespace` scopes the tokens and vault keys. The pipeline passes the document id, so
    two documents never share a pseudonym; it defaults to the hash of the text itself,
    which keeps a bare `mask(text)` deterministic and self-contained — the property the
    tests and the eval harness rely on.

    Returns a `MaskResult` whose spans are sorted, disjoint, and carry both coordinate
    systems. The original text is never returned and never stored on the result: the
    masked copy travels to the model, and the original stays in the document row behind
    the ACL check.
    """
    seed = namespace if namespace is not None else content_hash_of(text)

    tokens: dict[tuple[PiiType, str], str] = {}
    taken: set[str] = set()
    pieces: list[str] = []
    spans: list[MaskedSpan] = []

    cursor = 0  # how far through the original we have copied
    masked_length = 0  # length of what we have appended, i.e. the masked cursor

    for hit in _select(text, detectors):
        pieces.append(text[cursor : hit.start])
        masked_length += hit.start - cursor

        identity = canonicalise(hit.pii_type, text[hit.start : hit.end])
        key = (hit.pii_type, identity)
        token = tokens.get(key)
        if token is None:
            token = _allocate_token(seed, hit.pii_type, identity, taken)
            tokens[key] = token
            taken.add(token)

        pieces.append(token)
        spans.append(
            MaskedSpan(
                pii_type=hit.pii_type,
                token=token,
                orig_start=hit.start,
                orig_end=hit.end,
                masked_start=masked_length,
                masked_end=masked_length + len(token),
                vault_key=_vault_key(seed, hit.pii_type, identity),
            )
        )
        masked_length += len(token)
        cursor = hit.end

    pieces.append(text[cursor:])
    return MaskResult(masked_text="".join(pieces), spans=spans)
