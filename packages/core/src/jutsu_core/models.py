"""Domain models shared across the ingestion pipeline.

Transcribed from spec §9. These are the contracts S3 (connectors), S4 (PII masking)
and S5 (chunking) implement against, so they land before any of them.

Nothing here performs IO or calls an LLM — this package is pure types plus the small
amount of logic that belongs with them (content hashing, offset translation).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from enum import StrEnum
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
    the original. `to_original` is the single correct bridge between the two — the
    implementation lands in S4, together with the ten tests §9.1 requires.
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
