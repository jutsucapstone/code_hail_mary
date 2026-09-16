"""The two pieces of Cited Q&A that need no database: the link gate and the claims statement.

The authorization itself is exercised against Postgres in `apps/api/tests/test_cited_qa_scope`;
what is pinned here is what a refactor could quietly change — which addresses become links,
which questions read claims at all, and the shape of the one statement that reads them.
"""

from __future__ import annotations

import uuid

import pytest
from jutsu_retrieval.claims import (
    CLAIM_INTENTS,
    CLAIM_LIMIT,
    CLAIMS_STATEMENT,
    INTENT_WINDOW,
    MAX_ANCHORS,
    claim_intents,
    search_claims,
)
from jutsu_retrieval.links import MAX_SOURCE_URI_CHARS, safe_source_uri
from jutsu_retrieval.search import (
    ACL_PREDICATE,
    KT_PACKAGE_PREDICATE,
    ORG_SCOPE_SQL,
    SUBJECT_PREDICATE,
)


class TestTheLinkGate:
    @pytest.mark.parametrize(
        "address",
        [
            "https://github.com/d-dev/astro-agent",
            "https://docs.google.com/document/d/abc/edit?usp=drivesdk",
            "http://intranet.example.com/wiki/Astro_Agent",
            "https://example.atlassian.net/browse/ASTRO-12",
            "HTTPS://Example.com/Path",
            "https://mail.google.com/mail/u/0/#all/abc",
        ],
    )
    def test_an_absolute_web_address_is_a_link_exactly_as_given(self, address: str) -> None:
        assert safe_source_uri(address) == address

    @pytest.mark.parametrize(
        "address",
        [
            "javascript:alert(1)",
            "JavaScript:alert(document.cookie)",
            "data:text/html,<script>alert(1)</script>",
            "file:///C:/Users/somebody/Documents/notes.txt",
            "/Users/somebody/maildir/inbox/1.",
            "C:\\corpus\\maildir\\inbox\\1.",
            "//evil.example/path",
            "mailto:someone@example.com",
            "ftp://files.example.com/report.pdf",
            "https://",
            "https:///no-host",
            "https://user:secret@example.com/",
            "https://user@example.com/",
            " https://example.com/leading-space",
            "https://example.com/trailing-space ",
            "https://example.com/with\nnewline",
            "https://example.com/with\ttab",
            "https://example.com/\x00null",
            "https://example.com/\u2028separator",
            "",
        ],
    )
    def test_anything_else_is_no_link_at_all(self, address: str) -> None:
        assert safe_source_uri(address) is None

    @pytest.mark.parametrize("value", [None, 42, b"https://example.com", ["https://example.com"]])
    def test_a_value_that_is_not_text_is_no_link(self, value: object) -> None:
        assert safe_source_uri(value) is None

    def test_an_address_past_the_bound_is_no_link(self) -> None:
        stem = "https://example.com/"
        at_bound = stem + "a" * (MAX_SOURCE_URI_CHARS - len(stem))
        assert safe_source_uri(at_bound) == at_bound
        assert safe_source_uri(at_bound + "a") is None

    def test_a_malformed_host_is_no_link_rather_than_an_error(self) -> None:
        assert safe_source_uri("https://[::1/unterminated") is None


class TestWhichQuestionsReadClaims:
    @pytest.mark.parametrize(
        ("question", "kinds"),
        [
            ("Which decisions did I make about Astro Agent?", ["decision"]),
            ("What am I responsible for?", ["responsibility"]),
            ("Who have I worked with on Astro Agent?", ["person"]),
            ("tell me about my project from github astro agent", ["project"]),
            ("What was agreed in the weekly sync meeting?", ["meeting", "decision"]),
            # Whole words only: "agree" is not a cue, "agreed" is.
            ("What did we agree in the weekly sync meeting?", ["meeting"]),
            ("Summarise what I know about Astro Agent", []),
            ("Where are my Astro Agent documents stored?", []),
        ],
    )
    def test_intent_words_name_kinds_of_claim(self, question: str, kinds: list[str]) -> None:
        assert claim_intents(question) == kinds

    def test_the_kinds_are_the_extraction_taxonomy(self) -> None:
        assert list(CLAIM_INTENTS) == ["project", "meeting", "person", "responsibility", "decision"]

    async def test_a_question_with_no_kind_and_no_passage_reads_nothing(self) -> None:
        class Untouchable:
            async def execute(self, *args: object, **kwargs: object) -> object:
                raise AssertionError("the claims arm queried the database for nothing")

        found = await search_claims(
            Untouchable(),  # type: ignore[arg-type]
            user_id=uuid.uuid4(),
            question="Summarise what I know about Astro Agent",
        )

        assert found == []


class TestTheClaimsStatementShape:
    def test_every_arm_and_the_projection_carry_the_callers_acl_and_tenant(self) -> None:
        # Two candidate arms and the final projection each compose the authorization.
        assert CLAIMS_STATEMENT.count(ACL_PREDICATE) == 3
        assert CLAIMS_STATEMENT.count(f"cl.org_id = {ORG_SCOPE_SQL}") == 3
        assert CLAIMS_STATEMENT.count(f"d.org_id = {ORG_SCOPE_SQL}") == 3
        assert CLAIMS_STATEMENT.count("d.superseded_by IS NULL") == 3
        assert f"r.org_id = {ORG_SCOPE_SQL}" in CLAIMS_STATEMENT

    def test_it_never_reads_a_knowledge_transfer_scope(self) -> None:
        assert KT_PACKAGE_PREDICATE not in CLAIMS_STATEMENT
        assert SUBJECT_PREDICATE not in CLAIMS_STATEMENT
        assert ":subject_principals" not in CLAIMS_STATEMENT
        assert ":package_id" not in CLAIMS_STATEMENT
        assert "kt_" not in CLAIMS_STATEMENT

    def test_candidates_are_chosen_by_equality_and_bounded(self) -> None:
        assert "cl.chunk_id = ANY(CAST(:anchors AS uuid[]))" in CLAIMS_STATEMENT
        assert "cl.claim_type = ANY(CAST(:intent_types AS text[]))" in CLAIMS_STATEMENT
        assert "LIMIT :window" in CLAIMS_STATEMENT
        assert CLAIMS_STATEMENT.rstrip().endswith("LIMIT :limit")
        # Full-text is a rank over the pool, never a candidate filter.
        assert "@@" not in CLAIMS_STATEMENT

    def test_only_finished_runs_count_and_the_latest_is_not_correlated_per_claim(self) -> None:
        assert "r.finished_at IS NOT NULL" in CLAIMS_STATEMENT
        assert "DISTINCT ON (r.stats_json->>'document_id')" in CLAIMS_STATEMENT
        assert "LIMIT 1)" not in CLAIMS_STATEMENT

    def test_the_bounds_are_the_documented_ones(self) -> None:
        assert (CLAIM_LIMIT, INTENT_WINDOW, MAX_ANCHORS) == (12, 200, 60)
