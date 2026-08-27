"""Contract tests for the domain models.

These guard the invariants later slices depend on. The ten-case PII offset suite (§9.1)
lives with the implementation in `test_pii.py`; what is tested here is only what the
models themselves promise about their own shape.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from jutsu_core import (
    AclEntry,
    Chunk,
    MaskedSpan,
    MaskResult,
    PiiType,
    RawDocument,
    SourceSystem,
    acl_hash_of,
    content_hash_of,
)
from pydantic import ValidationError


def _doc(body: str = "hello", **overrides: object) -> RawDocument:
    defaults: dict[str, object] = {
        "external_id": "e1",
        "source_system": SourceSystem.LOCAL,
        "title": "t",
        "body": body,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "acls": [AclEntry(principal_type="org", principal_id="org-1")],
    }
    return RawDocument(**(defaults | overrides))


class TestContentHash:
    def test_is_stable_across_instances(self) -> None:
        assert _doc().content_hash == _doc().content_hash

    def test_tracks_the_body(self) -> None:
        assert _doc("a").content_hash != _doc("b").content_hash

    def test_ignores_metadata(self) -> None:
        """Idempotency (§4.14) keys on content. Retitling must not re-ingest."""
        assert _doc(title="one").content_hash == _doc(title="two").content_hash

    def test_matches_the_standalone_helper(self) -> None:
        assert _doc("xyz").content_hash == content_hash_of("xyz")

    def test_hashes_characters_not_bytes_consistently(self) -> None:
        """Non-ASCII must hash deterministically — the corpus is not ASCII-only."""
        assert content_hash_of("नमस्ते 🙏") == content_hash_of("नमस्ते 🙏")
        assert content_hash_of("नमस्ते") != content_hash_of("नमस्त")


class TestAclHash:
    def test_is_order_independent(self) -> None:
        """A source returning grants in a new order is not a permission change."""
        a = AclEntry(principal_type="user", principal_id="u1")
        b = AclEntry(principal_type="group", principal_id="g1")
        assert acl_hash_of([a, b]) == acl_hash_of([b, a])

    def test_detects_a_revoked_grant(self) -> None:
        a = AclEntry(principal_type="user", principal_id="u1")
        b = AclEntry(principal_type="group", principal_id="g1")
        assert acl_hash_of([a, b]) != acl_hash_of([a])

    def test_empty_grant_set_is_representable(self) -> None:
        """No grants is a real state meaning "nobody" — not an error (§17 test 1)."""
        assert acl_hash_of([]) == acl_hash_of([])


class TestAclEntry:
    def test_permission_is_read_only(self) -> None:
        """§4.8 — JUTSU never requests a write scope, so no other value is valid."""
        with pytest.raises(ValidationError):
            AclEntry(principal_type="user", principal_id="u1", permission="write")


class TestMaskResult:
    def _span(self, o0: int, o1: int, m0: int, m1: int) -> MaskedSpan:
        return MaskedSpan(
            pii_type=PiiType.PERSON,
            token="[PERSON_A1]",
            orig_start=o0,
            orig_end=o1,
            masked_start=m0,
            masked_end=m1,
            vault_key="v1",
        )

    def test_accepts_sorted_disjoint_spans(self) -> None:
        result = MaskResult(
            masked_text="[PERSON_A1] met [PERSON_A2]",
            spans=[self._span(0, 5, 0, 11), self._span(10, 15, 16, 27)],
        )
        assert len(result.spans) == 2

    def test_rejects_overlapping_spans(self) -> None:
        """Every offset translation assumes disjoint, ordered spans."""
        with pytest.raises(ValidationError):
            MaskResult(
                masked_text="x",
                spans=[self._span(0, 10, 0, 10), self._span(5, 15, 5, 15)],
            )

    def test_rejects_unsorted_spans(self) -> None:
        with pytest.raises(ValidationError):
            MaskResult(
                masked_text="x",
                spans=[self._span(20, 25, 20, 25), self._span(0, 5, 0, 5)],
            )

    def test_rejects_inverted_span(self) -> None:
        with pytest.raises(ValidationError):
            self._span(10, 5, 0, 5)

    def test_no_pii_is_the_identity_case(self) -> None:
        result = MaskResult(masked_text="nothing sensitive here")
        assert result.spans == []


class TestChunk:
    def test_rejects_inverted_range(self) -> None:
        with pytest.raises(ValidationError):
            Chunk(ordinal=0, text="t", char_start=90, char_end=10, token_count=1)

    def test_rejects_negative_offsets(self) -> None:
        with pytest.raises(ValidationError):
            Chunk(ordinal=0, text="t", char_start=-1, char_end=10, token_count=1)

    def test_empty_range_is_allowed(self) -> None:
        """A zero-width chunk is degenerate but not malformed."""
        assert Chunk(ordinal=0, text="", char_start=7, char_end=7, token_count=0)
