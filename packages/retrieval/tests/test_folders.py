"""Folder search without a database: its authorization, bounds, gate and evidence.

`apps/api/tests/test_folders_in_ask.py` watches outcomes against Postgres, and reads the plan
the application role gets at scale. These pin what an outcome cannot show: which predicate
each statement carries, that folder words are matched by the leakproof equality migration 0025
indexes, that only a question asking where reads folders, and that a folder's evidence is
titles and never text.
"""

from __future__ import annotations

import importlib.util
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from jutsu_retrieval.folders import (
    FOLDER_LIMIT,
    FOLDERS_STATEMENT,
    SUBJECT_FOLDERS_STATEMENT,
    _group,
    folder_terms,
    search_folders,
    search_subject_folders,
)
from jutsu_retrieval.search import ACL_PREDICATE, KT_PACKAGE_PREDICATE, ORG_SCOPE_SQL
from jutsu_retrieval.terms import MAX_FOLDER_WORDS, folder_words
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "src"
    / "jutsu_db"
    / "migrations"
    / "versions"
    / "0025_document_folders.py"
)
NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
BOTH = pytest.mark.parametrize("statement", [FOLDERS_STATEMENT, SUBJECT_FOLDERS_STATEMENT])


class _RecordingOp:
    """Alembic's `op`, keeping what a migration creates and the SQL it executes."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.tables: list[str] = []
        self.indexes: list[tuple[str, str, tuple[str, ...]]] = []

    def execute(self, statement: object) -> None:
        self.statements.append(str(statement))

    def create_table(self, name: str, *columns: object, **kwargs: object) -> None:
        self.tables.append(name)

    def create_index(self, name: str, table: str, columns: list[str], **kwargs: object) -> None:
        self.indexes.append((name, table, tuple(columns)))

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None


def _migration() -> _RecordingOp:
    spec = importlib.util.spec_from_file_location("migration_0025", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = _RecordingOp()
    vars(module)["op"] = recorder
    module.upgrade()
    return recorder


def _row(path: str, title: str, *, age_days: float, rank: float) -> SimpleNamespace:
    return SimpleNamespace(
        folder_path=path,
        folder_uri=f"https://files.example/{path}",
        document_id=uuid4(),
        document_title=title,
        source_system="local",
        chunk_id=uuid4(),
        char_start=0,
        char_end=12,
        occurred_at=NOW - timedelta(days=age_days),
        rank=rank,
    )


class Untouchable:
    """A session any use of which fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the session was used: {name}")


class TestAuthorization:
    def test_a_callers_folders_carry_the_acl_predicate_and_nothing_else(self) -> None:
        assert f"AND {ACL_PREDICATE} " in FOLDERS_STATEMENT
        assert KT_PACKAGE_PREDICATE not in FOLDERS_STATEMENT

    def test_a_packages_folders_carry_the_package_predicate_and_never_the_acl(self) -> None:
        assert f"AND {KT_PACKAGE_PREDICATE} " in SUBJECT_FOLDERS_STATEMENT
        assert ACL_PREDICATE not in SUBJECT_FOLDERS_STATEMENT

    @BOTH
    def test_both_stay_in_the_sessions_tenant_on_current_versions(self, statement: str) -> None:
        assert f"d.org_id = {ORG_SCOPE_SQL}" in statement
        assert "d.superseded_by IS NULL" in statement
        assert "d.folder_path IS NOT NULL" in statement

    @BOTH
    def test_both_are_bounded_inside_the_sql(self, statement: str) -> None:
        assert statement.endswith("LIMIT :candidates")

    @BOTH
    def test_neither_reads_a_documents_text(self, statement: str) -> None:
        for column in ("body_original", "body_masked", "ch.text", "c.text"):
            assert column not in statement

    def test_no_folder_search_accepts_a_principal_set_or_a_tenant(self) -> None:
        for function in (search_folders, search_subject_folders):
            names = set(inspect.signature(function).parameters)
            widening = names & {"principals", "groups", "subject_principals", "org_id"}
            assert not widening, f"{function.__name__} accepts {sorted(widening)}"


class TestWordsMatchByEquality:
    """Row-level security decides this, not taste. Beneath a policy PostgreSQL uses only a
    leakproof operator as an index condition, and full-text `@@` is not one — so an index
    over the path served the owner and never the application role (ADR 0029)."""

    @BOTH
    def test_folder_words_are_matched_by_text_equality_in_the_tenant(self, statement: str) -> None:
        assert "w.word = ANY(CAST(:words AS text[]))" in statement
        assert f"w.org_id = {ORG_SCOPE_SQL}" in statement

    @BOTH
    def test_no_operator_the_application_role_cannot_index_is_used(self, statement: str) -> None:
        for operator in ("@@", "to_tsvector", " LIKE ", " ILIKE ", "&&", "@>", "similarity("):
            assert operator not in statement, operator

    def test_migration_0025_indexes_the_words_tenant_first_under_forced_rls(self) -> None:
        migration = _migration()

        assert "document_folder_words" in migration.tables
        assert (
            "ix_document_folder_words_word",
            "document_folder_words",
            ("org_id", "word", "document_id"),
        ) in migration.indexes
        assert "ALTER TABLE document_folder_words FORCE ROW LEVEL SECURITY" in migration.statements
        assert "REVOKE UPDATE ON document_folder_words FROM jutsu_app" in migration.statements
        assert not any("USING gin" in statement for statement in migration.statements)


class TestTheQuestionsWords:
    def test_a_location_question_keeps_only_the_words_that_name_a_folder(self) -> None:
        assert folder_terms("Where are A's Astro Agent documents stored?") == ["astro", "agent"]

    def test_a_question_naming_no_folder_has_no_words(self) -> None:
        assert folder_terms("Where are the files kept?") == []

    @pytest.mark.parametrize(
        "question",
        ["Tell me about the Astro Agent project", "Summarise the Astro Agent decisions"],
    )
    def test_a_question_that_does_not_ask_where_reads_no_folder(self, question: str) -> None:
        assert folder_terms(question) == []

    @pytest.mark.parametrize(
        "question",
        [
            "Which folder holds the Astro plan?",
            "Astro plan location?",
            "Where did Priya save the Astro plan?",
        ],
    )
    def test_any_location_word_opens_the_folder_search(self, question: str) -> None:
        assert "astro" in folder_terms(question)

    async def test_without_words_nothing_is_read(self) -> None:
        session = cast(AsyncSession, Untouchable())

        for question in ("Where is it kept?", "Tell me about the Astro Agent project"):
            assert await search_folders(session, user_id=uuid4(), question=question) == []
        assert (
            await search_subject_folders(
                session,
                subject_user_id=uuid4(),
                package_id=uuid4(),
                within=None,
                question="Which folder?",
            )
            == []
        )


class TestFolderWords:
    def test_a_path_is_the_words_a_person_types(self) -> None:
        assert folder_words("My Drive/Projects/Astro_Agent") == [
            "drive",
            "projects",
            "astro",
            "agent",
        ]

    def test_short_repeated_and_absent_words_are_not_recorded(self) -> None:
        assert folder_words("Q3/Q3 reviews/reviews") == ["reviews"]
        assert folder_words(None) == []
        assert folder_words("") == []

    def test_a_word_no_question_could_name_is_skipped(self) -> None:
        assert folder_words("a" * 65 + "/Handover") == ["handover"]

    def test_a_deep_path_is_bounded(self) -> None:
        path = "/".join(f"level{n:03d}" for n in range(MAX_FOLDER_WORDS + 10))

        assert len(folder_words(path)) == MAX_FOLDER_WORDS

    def test_a_path_and_a_question_read_one_folder_name_as_the_same_words(self) -> None:
        assert folder_words("Projects/Astro-Agent") == ["projects", "astro", "agent"]
        assert folder_terms("Where is Astro-Agent kept?") == ["astro", "agent"]


class TestOneEvidencePerFolder:
    def test_folders_keep_the_order_their_best_match_arrived_in(self) -> None:
        rows = [
            _row("Projects/Astro Agent", "plan.md", age_days=9, rank=2),
            _row("Archive/Astro", "old.md", age_days=1, rank=1),
            _row("Projects/Astro Agent", "risks.md", age_days=2, rank=2),
        ]

        evidence = _group(rows, FOLDER_LIMIT)

        assert [item.folder_path for item in evidence] == ["Projects/Astro Agent", "Archive/Astro"]
        assert evidence[0].score == 2.0

    def test_a_folder_is_cited_through_its_newest_document(self) -> None:
        rows = [
            _row("Projects/Astro Agent", "plan.md", age_days=9, rank=2),
            _row("Projects/Astro Agent", "risks.md", age_days=2, rank=2),
        ]

        (folder,) = _group(rows, FOLDER_LIMIT)

        newest = rows[1]
        assert (folder.chunk_id, folder.document_id) == (newest.chunk_id, newest.document_id)
        assert folder.document_title == "risks.md"
        assert (folder.char_start, folder.char_end) == (0, 12)
        assert folder.folder_uri == newest.folder_uri

    def test_a_folders_evidence_is_its_path_and_titles_only(self) -> None:
        rows = [
            _row("Projects/Astro Agent", "plan.md", age_days=9, rank=2),
            _row("Projects/Astro Agent", "risks.md", age_days=2, rank=2),
        ]

        (folder,) = _group(rows, FOLDER_LIMIT)

        assert (
            folder.text == "Folder: Projects/Astro Agent\nDocuments kept in it: plan.md; risks.md"
        )

    def test_a_folder_names_five_titles_and_counts_the_rest(self) -> None:
        rows = [_row("Big", f"doc-{n}.md", age_days=n, rank=1) for n in range(8)]

        (folder,) = _group(rows, FOLDER_LIMIT)

        assert folder.text == (
            "Folder: Big\nDocuments kept in it: "
            "doc-0.md; doc-1.md; doc-2.md; doc-3.md; doc-4.md; and 3 more"
        )

    def test_a_title_repeated_in_one_folder_is_named_once(self) -> None:
        rows = [
            _row("Shared", "same.md", age_days=1, rank=1),
            _row("Shared", "same.md", age_days=2, rank=1),
        ]

        (folder,) = _group(rows, FOLDER_LIMIT)

        assert folder.text.endswith("Documents kept in it: same.md")

    def test_at_most_the_limit_of_folders_is_returned(self) -> None:
        rows = [_row(f"Folder {n}", "a.md", age_days=1, rank=1) for n in range(FOLDER_LIMIT + 3)]

        assert len(_group(rows, FOLDER_LIMIT)) == FOLDER_LIMIT
