"""Retrieval's subject authorization mode, asserted by shape (ADR 0025).

The behaviour — B reads A's documents through a package and nothing else — is proven
end to end against Postgres in `apps/api/tests/test_kt_subject_scope.py`. These tests pin
the mechanism underneath it, the part a refactor could change while every outcome test
still passed: which grants the predicate honours, which parameter it binds, and that the
subject scan is the caller's scan with one conjunct swapped and nothing else.
"""

from __future__ import annotations

import inspect

import pytest
from jutsu_retrieval import SUBJECT_PREDICATE, fetch_subject_evidence, search_subject_chunks
from jutsu_retrieval.search import ACL_PREDICATE, _statement

SHAPES = [(paginated, windowed) for paginated in (False, True) for windowed in (False, True)]


class TestTheSubjectPredicate:
    def test_it_honours_exactly_one_grant_the_direct_user_grant(self) -> None:
        """A group arm would carry the subject's teams; an org arm, the whole tenant."""
        assert "principal_type = 'user'" in SUBJECT_PREDICATE
        assert "'group'" not in SUBJECT_PREDICATE
        assert "'org'" not in SUBJECT_PREDICATE
        assert "current_org_id" not in SUBJECT_PREDICATE

    def test_it_binds_its_own_parameter_and_never_the_callers(self) -> None:
        """Distinct binds, so swapping one predicate for the other fails to bind rather
        than quietly authorizing the wrong person."""
        assert ":subject_principals" in SUBJECT_PREDICATE
        assert ":principals" not in SUBJECT_PREDICATE.replace(":subject_principals", "")
        assert ":groups" not in SUBJECT_PREDICATE

    def test_it_is_a_read_grant_on_the_document_being_scanned(self) -> None:
        assert "a.document_id = d.id" in SUBJECT_PREDICATE
        assert "a.permission = 'read'" in SUBJECT_PREDICATE


class TestTheSubjectStatement:
    @pytest.mark.parametrize(("paginated", "windowed"), SHAPES)
    def test_the_callers_statement_is_byte_identical_to_before(
        self, paginated: bool, windowed: bool
    ) -> None:
        assert _statement(paginated=paginated, windowed=windowed) == _statement(
            paginated=paginated, windowed=windowed, subject=False
        )

    @pytest.mark.parametrize(("paginated", "windowed"), SHAPES)
    def test_the_subject_scan_swaps_the_authorization_conjunct_and_nothing_else(
        self, paginated: bool, windowed: bool
    ) -> None:
        caller = _statement(paginated=paginated, windowed=windowed)
        subject = _statement(paginated=paginated, windowed=windowed, subject=True)

        assert caller.count(ACL_PREDICATE) == 1
        assert ACL_PREDICATE not in subject
        assert subject == caller.replace(ACL_PREDICATE, SUBJECT_PREDICATE)

    def test_the_measured_performance_shape_holds_for_the_subject_scan(self) -> None:
        """`chunks` alone in the FROM and distance alone in the ORDER BY — the two
        100x cliffs CLAUDE.md records hold for this scan exactly as for the caller's."""
        inner = _statement(paginated=False, subject=True).split(") SELECT h.id")[0]

        assert "FROM chunks c " in inner
        assert " JOIN " not in inner
        assert inner.endswith("ORDER BY c.embedding <=> CAST(:query AS vector) LIMIT :k")

    def test_the_window_sits_inside_the_documents_exists_beside_the_predicate(self) -> None:
        inner = _statement(paginated=False, windowed=True, subject=True).split(") SELECT h.id")[0]

        assert inner.index(SUBJECT_PREDICATE) < inner.index("d.created_at >=")


class TestTheSignatures:
    @pytest.mark.parametrize("function", [search_subject_chunks, fetch_subject_evidence])
    def test_nothing_that_names_an_authorization_is_a_parameter(self, function: object) -> None:
        """The subject is a user id resolved inside; no principal set, no group set, no
        tenant and no requester can be handed in to widen or redirect it."""
        parameters = set(inspect.signature(function).parameters)  # type: ignore[arg-type]

        assert "subject_user_id" in parameters
        for forbidden in ("org_id", "principals", "groups", "subject_principals", "user_id"):
            assert forbidden not in parameters
