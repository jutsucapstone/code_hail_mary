"""Retrieval's package authorization mode, asserted by shape (ADR 0025, ADR 0027).

The behaviour — B reads A's package and nothing else — is proven end to end against
Postgres in `apps/api/tests/test_kt_subject_scope.py` and `test_kt_package_boundary.py`.
These tests pin the mechanism underneath it, the part a refactor could change while every
outcome test still passed: which grants the predicates honour, which parameters they bind,
and that the package scan is the caller's scan with one conjunct swapped and nothing else.
"""

from __future__ import annotations

import inspect

import pytest
from jutsu_retrieval import (
    KT_PACKAGE_PREDICATE,
    KT_PACKAGE_RULE,
    SUBJECT_PREDICATE,
    fetch_subject_evidence,
    search_subject_chunks,
)
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


class TestThePackagePredicate:
    def test_the_rule_starts_from_the_subjects_own_documents_and_goes_no_wider(self) -> None:
        assert KT_PACKAGE_RULE.startswith(SUBJECT_PREDICATE)
        assert ACL_PREDICATE not in KT_PACKAGE_RULE
        assert "principal_type = 'group'" not in KT_PACKAGE_RULE
        assert "principal_type = 'org'" not in KT_PACKAGE_RULE
        assert "current_org_id" not in KT_PACKAGE_RULE

    def test_a_basket_file_needs_a_live_attachment_to_this_package(self) -> None:
        """Owning an upload is not enough (ADR 0027): attached, not detached, not deleted."""
        assert "FROM kt_package_files kf" in KT_PACKAGE_RULE
        assert "kf.package_id = CAST(:package_id AS uuid)" in KT_PACKAGE_RULE
        assert "kf.detached_at IS NULL" in KT_PACKAGE_RULE
        assert "kb.deleted_at IS NULL" in KT_PACKAGE_RULE
        assert "CAST(kb.id AS text) = d.external_id" in KT_PACKAGE_RULE
        assert "ks.system = 'basket'" in KT_PACKAGE_RULE

    def test_the_period_binds_documents_from_connected_applications(self) -> None:
        assert "d.created_at >= CAST(:window_start AS timestamptz)" in KT_PACKAGE_RULE
        assert "d.created_at <= CAST(:window_end AS timestamptz)" in KT_PACKAGE_RULE

    def test_the_predicate_is_the_rule_minus_this_packages_exclusions(self) -> None:
        assert KT_PACKAGE_PREDICATE.startswith(KT_PACKAGE_RULE)
        tail = KT_PACKAGE_PREDICATE[len(KT_PACKAGE_RULE) :]
        assert tail.startswith(" AND NOT EXISTS (SELECT 1 FROM kt_package_exclusions kx ")
        assert "kx.package_id = CAST(:package_id AS uuid)" in tail
        # The stable identity, so a new version of an excluded document stays excluded.
        assert "kx.source_id = d.source_id AND kx.external_id = d.external_id" in tail

    def test_it_never_binds_the_callers_parameters(self) -> None:
        unbound = KT_PACKAGE_PREDICATE.replace(":subject_principals", "")
        assert ":principals" not in unbound
        assert ":groups" not in unbound


class TestTheSubjectStatement:
    @pytest.mark.parametrize(("paginated", "windowed"), SHAPES)
    def test_the_callers_statement_is_byte_identical_to_before(
        self, paginated: bool, windowed: bool
    ) -> None:
        assert _statement(paginated=paginated, windowed=windowed) == _statement(
            paginated=paginated, windowed=windowed, subject=False
        )

    @pytest.mark.parametrize("paginated", [False, True])
    def test_the_package_scan_swaps_the_authorization_conjunct_and_nothing_else(
        self, paginated: bool
    ) -> None:
        caller = _statement(paginated=paginated)
        package = _statement(paginated=paginated, subject=True)

        assert caller.count(ACL_PREDICATE) == 1
        assert ACL_PREDICATE not in package
        assert package == caller.replace(ACL_PREDICATE, KT_PACKAGE_PREDICATE)

    @pytest.mark.parametrize("paginated", [False, True])
    def test_the_package_scan_carries_its_own_period(self, paginated: bool) -> None:
        """A separate `_WINDOW` would bind attached basket files to the period as well."""
        assert _statement(paginated=paginated, windowed=True, subject=True) == _statement(
            paginated=paginated, subject=True
        )

    def test_the_measured_performance_shape_holds_for_the_package_scan(self) -> None:
        """`chunks` alone in the FROM and distance alone in the ORDER BY — the two
        100x cliffs CLAUDE.md records hold for this scan exactly as for the caller's."""
        inner = _statement(paginated=False, subject=True).split(") SELECT h.id")[0]
        outer_from = inner.split(" WHERE ", 1)[0]

        assert outer_from.endswith("FROM chunks c")
        assert inner.endswith("ORDER BY c.embedding <=> CAST(:query AS vector) LIMIT :k")

    def test_the_window_sits_inside_the_documents_exists_beside_the_predicate(self) -> None:
        inner = _statement(paginated=False, windowed=True, subject=True).split(") SELECT h.id")[0]

        assert inner.index(SUBJECT_PREDICATE) < inner.index("d.created_at >=")


class TestTheSignatures:
    @pytest.mark.parametrize("function", [search_subject_chunks, fetch_subject_evidence])
    def test_nothing_that_names_an_authorization_is_a_parameter(self, function: object) -> None:
        """The subject is a user id resolved inside and the package an id the predicate
        narrows by; no principal set, no group set, no tenant and no requester can be
        handed in to widen or redirect it."""
        parameters = set(inspect.signature(function).parameters)  # type: ignore[arg-type]

        assert "subject_user_id" in parameters
        assert "package_id" in parameters
        for forbidden in ("org_id", "principals", "groups", "subject_principals", "user_id"):
            assert forbidden not in parameters
