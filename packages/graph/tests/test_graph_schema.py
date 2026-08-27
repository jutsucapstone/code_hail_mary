"""Migrations, the ledger, and the constraints they create (§4.12, §7).

Run against a real Neo4j. Constraints that exist in a listing and do not enforce are the
exact failure this repository has already had once, in Postgres — ADR 0003 records a
whole slice spent on policies that were present and inert. So these tests do not check
that a constraint appears in `SHOW CONSTRAINTS`; they check that a violating write
raises.

These tests mutate database-wide schema. Each one restores it, and `schema` restores it
again on teardown, so ordering between modules cannot matter.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from graph_support import skip_without_graph
from jutsu_graph.driver import GraphSettings, close_driver, ddl_session, write_session
from jutsu_graph.labels import NodeLabel, RelationshipType
from jutsu_graph.migrations import (
    LEDGER_LABEL,
    ChecksumMismatch,
    MissingDownMigration,
    applied_versions,
    downgrade,
    load_migrations,
    split_statements,
    upgrade,
)
from neo4j.exceptions import ConstraintError

EXPECTED_CONSTRAINTS = {"person_org_email", "doc_source_id", "project_key"}
EXPECTED_INDEXES = {"decision_time", "topic_name", "decision_text"}


@pytest.fixture(name="schema")
async def schema_fixture() -> AsyncIterator[GraphSettings]:
    """A graph at head, restored to head afterwards whatever the test did."""
    skip_without_graph()
    from jutsu_graph.driver import get_graph_settings

    settings = get_graph_settings()
    await upgrade(settings=settings)
    try:
        yield settings
    finally:
        await upgrade(settings=settings)
        await close_driver()


async def schema_objects(settings: GraphSettings) -> dict[str, set[str]]:
    """Names of every constraint and index, as the fingerprint for a round trip."""
    async with ddl_session(settings=settings) as session:
        constraints = await session.run("SHOW CONSTRAINTS YIELD name RETURN name")
        indexes = await session.run("SHOW INDEXES YIELD name RETURN name")
    return {
        "constraints": {row["name"] for row in constraints},
        "indexes": {row["name"] for row in indexes},
    }


# --------------------------------------------------------------------------- loading


class TestMigrationFiles:
    def test_the_shipped_migrations_load(self) -> None:
        migrations = load_migrations()
        assert [migration.version for migration in migrations] == ["001"]
        assert migrations[0].name == "constraints"

    def test_every_migration_has_a_down_file(self) -> None:
        """Checked at load, so `upgrade` cannot apply something it could not reverse."""
        for migration in load_migrations():
            assert migration.down.strip(), migration.version

    def test_a_missing_down_file_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "002_orphan.up.cypher").write_text("RETURN 1;", encoding="utf-8")
        with pytest.raises(MissingDownMigration, match="reversible"):
            load_migrations(tmp_path)

    def test_an_unnumbered_file_is_refused(self, tmp_path: Path) -> None:
        """Order and direction have to be unambiguous from the name alone."""
        (tmp_path / "constraints.cypher").write_text("RETURN 1;", encoding="utf-8")
        with pytest.raises(ValueError, match="ambiguous"):
            load_migrations(tmp_path)

    def test_the_checksum_tracks_the_up_file(self, tmp_path: Path) -> None:
        for name, body in (("001_a.up.cypher", "RETURN 1;"), ("001_a.down.cypher", "RETURN 2;")):
            (tmp_path / name).write_text(body, encoding="utf-8")
        first = load_migrations(tmp_path)[0].checksum

        (tmp_path / "001_a.up.cypher").write_text("RETURN 3;", encoding="utf-8")
        assert load_migrations(tmp_path)[0].checksum != first


class TestStatementSplitting:
    def test_comments_are_stripped(self) -> None:
        assert split_statements("// a comment\nRETURN 1;") == ["RETURN 1"]

    def test_a_semicolon_inside_a_comment_does_not_split(self) -> None:
        assert split_statements("// one; two\nRETURN 1;") == ["RETURN 1"]

    def test_blank_statements_are_dropped(self) -> None:
        assert split_statements("RETURN 1;;\n\n;") == ["RETURN 1"]

    def test_multiple_statements_split(self) -> None:
        assert split_statements("RETURN 1;\nRETURN 2;") == ["RETURN 1", "RETURN 2"]


# --------------------------------------------------------------------------- applying


class TestUpgradeAndLedger:
    async def test_upgrade_applies_from_empty(self, schema: GraphSettings) -> None:
        await downgrade(settings=schema)
        assert await applied_versions(settings=schema) == []

        assert await upgrade(settings=schema) == ["001"]
        assert await applied_versions(settings=schema) == ["001"]

    async def test_upgrade_is_idempotent(self, schema: GraphSettings) -> None:
        """The M1 discipline: a second run adds nothing, proven rather than assumed."""
        before = await schema_objects(schema)
        assert await upgrade(settings=schema) == []
        assert await schema_objects(schema) == before

    async def test_upgrade_stops_at_a_target(self, schema: GraphSettings) -> None:
        await downgrade(settings=schema)
        assert await upgrade(target="000", settings=schema) == []
        assert await applied_versions(settings=schema) == []

    async def test_editing_an_applied_migration_raises(self, schema: GraphSettings) -> None:
        """Two environments reporting the same version with different schemas is the
        failure this prevents. The fix is a new migration, never an edit."""
        async with ddl_session(settings=schema) as session:
            await session.run(
                f"MATCH (m:{LEDGER_LABEL} {{version: '001'}}) SET m.checksum = $bogus",
                bogus="not-the-real-checksum",
            )
        try:
            with pytest.raises(ChecksumMismatch, match="has changed since"):
                await upgrade(settings=schema)
        finally:
            # Undone here rather than left to the fixture. A poisoned ledger is exactly
            # what `upgrade` refuses to touch, so the teardown's own `upgrade` would
            # raise and every later test in the session would error on setup.
            await downgrade(settings=schema)


class TestDowngrade:
    async def test_downgrade_then_upgrade_restores_the_schema(self, schema: GraphSettings) -> None:
        """The Postgres suite proves this with a `pg_catalog` fingerprint. Same idea."""
        before = await schema_objects(schema)
        assert EXPECTED_CONSTRAINTS <= before["constraints"]
        assert EXPECTED_INDEXES <= before["indexes"]

        assert await downgrade(settings=schema) == ["001"]
        during = await schema_objects(schema)
        assert not (EXPECTED_CONSTRAINTS & during["constraints"])
        assert not (EXPECTED_INDEXES & during["indexes"])

        assert await upgrade(settings=schema) == ["001"]
        assert await schema_objects(schema) == before

    async def test_downgrade_clears_the_ledger(self, schema: GraphSettings) -> None:
        await downgrade(settings=schema)
        assert await applied_versions(settings=schema) == []

    async def test_downgrade_to_a_target_keeps_it(self, schema: GraphSettings) -> None:
        assert await downgrade(target="001", settings=schema) == []
        assert await applied_versions(settings=schema) == ["001"]


# --------------------------------------------------------------------------- enforcing


class TestConstraintsEnforce:
    async def test_duplicate_person_within_one_org_is_rejected(self, schema: GraphSettings) -> None:
        """Present in a listing is not the same as enforcing. This writes twice."""
        org = uuid.uuid4()
        try:
            async with write_session(org, settings=schema) as session:
                await session.run(
                    "CREATE (p:Person {org_id: $org_id, email: $email})", email="a@example.com"
                )
            with pytest.raises(ConstraintError):
                async with write_session(org, settings=schema) as session:
                    await session.run(
                        "CREATE (p:Person {org_id: $org_id, email: $email})",
                        email="a@example.com",
                    )
        finally:
            async with write_session(org, settings=schema) as session:
                await session.run("MATCH (n) WHERE n.org_id = $org_id DETACH DELETE n")

    async def test_the_same_address_in_two_orgs_is_allowed(self, schema: GraphSettings) -> None:
        """The constraint is compound on org_id, which is what makes it multi-tenant.

        A uniqueness constraint on email alone would mean one tenant hiring somebody
        blocked another tenant from ever recording them.
        """
        org_a, org_b = uuid.uuid4(), uuid.uuid4()
        try:
            for org in (org_a, org_b):
                async with write_session(org, settings=schema) as session:
                    await session.run(
                        "CREATE (p:Person {org_id: $org_id, email: $email})",
                        email="shared@example.com",
                    )
        finally:
            for org in (org_a, org_b):
                async with write_session(org, settings=schema) as session:
                    await session.run("MATCH (n) WHERE n.org_id = $org_id DETACH DELETE n")

    async def test_duplicate_project_key_within_one_org_is_rejected(
        self, schema: GraphSettings
    ) -> None:
        org = uuid.uuid4()
        try:
            async with write_session(org, settings=schema) as session:
                await session.run("CREATE (p:Project {org_id: $org_id, key: $key})", key="FALCON")
            with pytest.raises(ConstraintError):
                async with write_session(org, settings=schema) as session:
                    await session.run(
                        "CREATE (p:Project {org_id: $org_id, key: $key})", key="FALCON"
                    )
        finally:
            async with write_session(org, settings=schema) as session:
                await session.run("MATCH (n) WHERE n.org_id = $org_id DETACH DELETE n")


# --------------------------------------------------------------------------- allowlist


class TestLedgerIsNotApplicationWritable:
    def test_the_ledger_label_is_absent_from_the_allowlist(self) -> None:
        """The record of which migrations ran is not something a query builder may edit."""
        assert LEDGER_LABEL not in {label.value for label in NodeLabel}
        assert LEDGER_LABEL not in {rel.value for rel in RelationshipType}
