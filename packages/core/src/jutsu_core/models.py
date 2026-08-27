"""Domain models shared across the ingestion pipeline.

Transcribed from spec §9. These are the contracts S3 (connectors), S4 (PII masking)
and S5 (chunking) implement against, so they land before any of them.

Nothing here performs IO or calls an LLM — this package is pure types plus the small
amount of logic that belongs with them (content hashing, offset translation).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from enum import StrEnum
from functools import cached_property
from hashlib import sha256
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, computed_field, model_validator

__all__ = [
    "AclEntry",
    "Chunk",
    "Connector",
    "MaskResult",
    "MaskedSpan",
    "PiiType",
    "RawDocument",
    "SourceSystem",
]


class SourceSystem(StrEnum):
    """Every system JUTSU can read from. Read-only, always (§4.8)."""

    LOCAL = "local"
    GMAIL = "gmail"
    M365 = "m365"
    SLACK = "slack"
    JIRA = "jira"
    CONFLUENCE = "confluence"
    GITHUB = "github"


class AclEntry(BaseModel):
    """A single read grant, captured verbatim from the source system.

    `permission` is fixed to "read" by design: JUTSU never requests a write scope, so
    there is no other permission it could legitimately hold (§4.8).
    """

    principal_type: Literal["user", "group", "org", "public"]
    principal_id: str
    permission: Literal["read"] = "read"


class RawDocument(BaseModel):
    """A document as fetched from a source, before masking or chunking.

    `body` is the ORIGINAL text. Chunk offsets and citation spans are measured against
    it, never against the masked variant (§9.2).
    """

    external_id: str
    source_system: SourceSystem
    uri: str | None = None
    title: str
    body: str
    mime: str = "text/plain"
    author_external_id: str | None = None
    participant_external_ids: list[str] = Field(default_factory=list)
    thread_id: str | None = None
    created_at: datetime
    modified_at: datetime | None = None
    acls: list[AclEntry]
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        """Half of the idempotency key (§4.14).

        Hashes the original body only. Deliberately excludes metadata: a document whose
        title was edited but whose text is identical must not re-ingest as new content.
        """
        return sha256(self.body.encode("utf-8")).hexdigest()


@runtime_checkable
class Connector(Protocol):
    """The only interface a source system implements. Read-only by construction."""

    system: SourceSystem

    def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        """External ids changed since `cursor`, oldest first."""
        ...

    async def fetch(self, external_id: str) -> RawDocument: ...

    async def acls(self, external_id: str) -> list[AclEntry]: ...


class PiiType(StrEnum):
    PERSON = "person"
    EMAIL = "email"
    PHONE = "phone"
    ADDRESS = "address"
    GOV_ID = "gov_id"
    FINANCIAL = "financial"


class MaskedSpan(BaseModel):
    """One replaced run of text, with both coordinate systems recorded.

    All offsets are CHARACTER offsets into Python `str`, never byte offsets — the
    corpus contains non-ASCII text and a byte index silently mis-highlights it (§9.1).
    """

    pii_type: PiiType
    token: str
    orig_start: int = Field(ge=0)
    orig_end: int = Field(ge=0)
    masked_start: int = Field(ge=0)
    masked_end: int = Field(ge=0)
    vault_key: str

    @model_validator(mode="after")
    def _ends_follow_starts(self) -> MaskedSpan:
        if self.orig_end < self.orig_start:
            raise ValueError("orig_end precedes orig_start")
        if self.masked_end < self.masked_start:
            raise ValueError("masked_end precedes masked_start")
        return self


class MaskResult(BaseModel):
    """Masked text plus the map back to the original.

    The LLM only ever sees `masked_text`; the citation UI only ever highlights spans in
    the original. `to_original` is the single correct bridge between the two, and it is
    the only sanctioned one — a caller that adds and subtracts lengths by hand is writing
    the off-by-one this class exists to prevent.

    **Both coordinate systems are character offsets into a Python `str`.** Never bytes.
    The corpus is not ASCII, and a byte index silently mis-highlights every citation in a
    Devanagari or emoji-bearing document (§9.1).
    """

    masked_text: str
    spans: list[MaskedSpan] = Field(default_factory=list)

    @model_validator(mode="after")
    def _spans_sorted_and_disjoint(self) -> MaskResult:
        """Ordering and non-overlap are relied on by every offset translation."""
        previous_orig_end = -1
        previous_masked_end = -1
        for span in self.spans:
            if span.orig_start < previous_orig_end:
                raise ValueError("spans overlap or are not sorted by orig_start")
            if span.masked_start < previous_masked_end:
                raise ValueError("spans overlap or are not sorted by masked_start")
            previous_orig_end = span.orig_end
            previous_masked_end = span.masked_end
        return self

    # ------------------------------------------------------------------ offset map
    #
    # The translation is a step function. Outside every span the two coordinate systems
    # differ by a constant, and that constant only moves where a replacement had a
    # different length from the text it replaced. So the question is "which span do I
    # fall inside, or after", which is a bisect over the starts rather than a walk over
    # the list — a pilot document carries hundreds of spans and the chunker converts
    # every boundary it produces.
    #
    # Cached: `spans` is validated sorted and disjoint at construction, and nothing
    # mutates this model afterwards.

    @cached_property
    def _masked_starts(self) -> list[int]:
        return [span.masked_start for span in self.spans]

    @cached_property
    def _orig_starts(self) -> list[int]:
        return [span.orig_start for span in self.spans]

    def to_original(self, masked_offset: int) -> int:
        """Character offset in `masked_text` → character offset in the original body.

        An offset *inside* a token is ambiguous by construction: `[PERSON_A7]` stands in
        for a run of original text of some other length, and there is no
        position-by-position correspondence between the two. Such an offset resolves to
        the **start** of the run it lands in, which keeps the function monotonic and
        never returns a position part-way through somebody else's name. `masked_end` is
        not inside the token — it is the position after it — and maps to `orig_end`.

        Use `original_range` for a slice. It applies start semantics to one end and end
        semantics to the other, so the result always covers the whole of any entity the
        slice touched.
        """
        if not 0 <= masked_offset <= len(self.masked_text):
            raise ValueError("masked_offset is outside masked_text")

        index = bisect_right(self._masked_starts, masked_offset) - 1
        if index < 0:
            # Before the first replacement, so nothing has shifted yet.
            return masked_offset

        span = self.spans[index]
        if masked_offset >= span.masked_end:
            return span.orig_end + (masked_offset - span.masked_end)
        return span.orig_start

    def to_masked(self, orig_offset: int) -> int:
        """Character offset in the original body → character offset in `masked_text`.

        The mirror of `to_original`, with the same rule for an offset landing inside a
        replaced run: it resolves to the start of the token that replaced it.

        Only the lower bound is checked. The original text is deliberately not carried on
        this model — it is the one string that must never travel alongside the masked
        copy — so there is no length here to validate an upper bound against.
        """
        if orig_offset < 0:
            raise ValueError("orig_offset is negative")

        index = bisect_right(self._orig_starts, orig_offset) - 1
        if index < 0:
            return orig_offset

        span = self.spans[index]
        if orig_offset >= span.orig_end:
            return span.masked_end + (orig_offset - span.orig_end)
        return span.masked_start

    def original_range(self, masked_start: int, masked_end: int) -> tuple[int, int]:
        """The smallest original range that fully covers a masked slice.

        This is what the chunker stores. §9.2 forbids splitting inside a `MaskedSpan`, so
        in a correct pipeline both boundaries already sit outside every span and this is
        two `to_original` calls. It rounds outward regardless, because the alternative
        when that guarantee is broken is a citation that highlights half a masked entity
        — or, when both boundaries land in one token, an inverted range.
        """
        if masked_end < masked_start:
            raise ValueError("masked_end precedes masked_start")
        if not 0 <= masked_end <= len(self.masked_text):
            raise ValueError("masked_end is outside masked_text")

        start = self.to_original(masked_start)

        # bisect_LEFT, and the difference is a real bug rather than a style choice.
        # `to_original` uses bisect_right because an offset sitting exactly on a token's
        # `masked_start` is the first character *of* that token. As an end offset the
        # same position means the opposite — the slice stops just before the token — and
        # bisect_right would pull that span in and round the range out to its far end,
        # so a slice ending immediately before an email address would be recorded as
        # covering the whole address.
        index = bisect_left(self._masked_starts, masked_end) - 1
        if index < 0:
            return start, masked_end

        span = self.spans[index]
        if masked_end >= span.masked_end:
            return start, span.orig_end + (masked_end - span.masked_end)
        return start, span.orig_end


class Chunk(BaseModel):
    """An embedding unit.

    `text` is masked (it is what gets embedded and shown to the model) while
    `char_start`/`char_end` index the ORIGINAL document. Mixing those two up is the
    trap called out in §22 — it mis-highlights every citation.
    """

    ordinal: int = Field(ge=0)
    text: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    token_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _end_follows_start(self) -> Chunk:
        if self.char_end < self.char_start:
            raise ValueError("char_end precedes char_start")
        return self


def content_hash_of(body: str) -> str:
    """Standalone hash, for callers holding text without a RawDocument."""
    return sha256(body.encode("utf-8")).hexdigest()


def acl_hash_of(acls: Sequence[AclEntry]) -> str:
    """Stable hash of a grant set, for detecting permission drift between syncs.

    Sorted so that a source returning the same grants in a different order does not
    look like a change.
    """
    parts = sorted(f"{a.principal_type}:{a.principal_id}:{a.permission}" for a in acls)
    return sha256("\n".join(parts).encode("utf-8")).hexdigest()
