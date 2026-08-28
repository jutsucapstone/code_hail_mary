"""Migration reversibility and schema shape.

M1 requires migrations to be reversible. Asserting that `downgrade` exited zero proves
almost nothing — a downgrade that drops a table but leaves its index, enum or policy
behind exits zero and leaves the database subtly different.

So the check is a *fingerprint*: snapshot the schema from `pg_catalog`, round-trip
through downgrade and upgrade, snapshot again, and compare. Anything the migration
fails to reverse shows up as a diff.
"""

from __future__ import annotations

import json
import uuid

import pytest
from jutsu_db import EMBEDDING_DIM
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from conftest import alembic_config, run_alembic

# Columns, constraints, indexes, enum labels and policies — everything a downgrade
# could plausibly leave behind. Ordered so the comparison is stable across runs.
FINGERPRINT_SQL = """
SELECT json_build_object(
  'columns', (
    SELECT coalesce(json_agg(x ORDER BY x::text), '[]'::json) FROM (
      SELECT table_name, column_name, data_type, is_nullable, column_default
      FROM information_schema.columns WHERE table_schema = 'public'
    ) x
  ),
  'constraints', (
    SELECT coalesce(json_agg(x ORDER BY x::text), '[]'::json) FROM (
      SELECT conname, contype, pg_get_constraintdef(oid) AS def
      FROM pg_constraint
      WHERE connamespace = 'public'::regnamespace
    ) x
  ),
  'indexes', (
    SELECT coalesce(json_agg(x ORDER BY x::text), '[]'::json) FROM (
      SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'
    ) x
  ),
  'policies', (
    SELECT coalesce(json_agg(x ORDER BY x::text), '[]'::json) FROM (
      SELECT tablename, policyname, qual, with_check FROM pg_policies WHERE schemaname = 'public'
    ) x
  ),
  'rls', (
    SELECT coalesce(json_agg(x ORDER BY x::text), '[]'::json) FROM (
      SELECT relname, relrowsecurity, relforcerowsecurity
      FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'
    ) x
  ),
  'enums', (
    SELECT coalesce(json_agg(x ORDER BY x::text), '[]'::json) FROM (
      SELECT t.typname, e.enumlabel
      FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
    ) x
  )
)
"""


async def _scope(conn: AsyncConnection, org_id: uuid.UUID) -> None:
    await conn.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )


async def _fingerprint(conn: AsyncConnection) -> dict[str, object]:
    raw = (await conn.execute(text(FINGERPRINT_SQL))).scalar_one()
    return raw if isinstance(raw, dict) else json.loads(raw)


class TestReversibility:
    async def test_downgrade_then_upgrade_restores_identical_schema(
        self, migrated: str, migration_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DDL and pg_catalog reads both go through the owner: the application role
        # deliberately cannot run DDL.
        monkeypatch.setenv("DATABASE_URL", migration_url)
        cfg = alembic_config(migration_url)

        engine = create_async_engine(migration_url)
        try:
            async with engine.connect() as conn:
                before = await _fingerprint(conn)

            await run_alembic(cfg, "downgrade", "base")
            await run_alembic(cfg, "upgrade", "head")

            async with engine.connect() as conn:
                after = await _fingerprint(conn)
        finally:
            await engine.dispose()

        for section in ("columns", "constraints", "indexes", "policies", "rls", "enums"):
            assert before[section] == after[section], f"{section} differs after round trip"

    async def test_downgrade_leaves_no_domain_tables(
        self, migrated: str, migration_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A partial downgrade is worse than none — the next upgrade then fails on a
        table that already exists."""
        monkeypatch.setenv("DATABASE_URL", migration_url)
        cfg = alembic_config(migration_url)
        await run_alembic(cfg, "downgrade", "base")

        engine = create_async_engine(migration_url)
        try:
            async with engine.connect() as conn:
                remaining = (
                    (
                        await conn.execute(
                            text(
                                "SELECT tablename FROM pg_tables "
                                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await engine.dispose()

        assert remaining == []
        await run_alembic(cfg, "upgrade", "head")  # restore for the fixture teardown


class TestSchemaShape:
    async def test_pgvector_extension_is_installed(self, conn: AsyncConnection) -> None:
        installed = (
            await conn.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'"))
        ).scalar_one()
        assert installed == 1

    async def test_hnsw_index_exists_with_spec_parameters(self, conn: AsyncConnection) -> None:
        """§8 pins m=16, ef_construction=64. Recall depends on them, so a silent change
        would move eval numbers with no code diff to point at."""
        definition = (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chunks_embedding_hnsw'")
            )
        ).scalar_one()
        assert "USING hnsw" in definition
        assert "vector_cosine_ops" in definition
        assert "m='16'" in definition or "m=16" in definition
        assert "ef_construction='64'" in definition or "ef_construction=64" in definition

    async def test_embedding_column_has_the_expected_width(self, conn: AsyncConnection) -> None:
        """A mismatch here is only discovered when the first insert fails, long after
        the embeddings have been paid for."""
        dim = (
            await conn.execute(
                text(
                    "SELECT a.atttypmod FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "WHERE c.relname = 'chunks' AND a.attname = 'embedding'"
                )
            )
        ).scalar_one()
        assert dim == EMBEDDING_DIM

    async def test_langgraph_schema_is_separate(self, conn: AsyncConnection) -> None:
        """§8 — checkpoints never mix with domain tables."""
        exists = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'langgraph'"
                )
            )
        ).scalar_one()
        assert exists == 1

    async def test_versions_can_coexist_but_only_one_is_current(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The partial index, exercised rather than read out of the catalogue.

        Two rows sharing `(org_id, source_id, external_id)` are legal as long as one is
        superseded, and a second *current* row is refused. That pair is the whole
        versioning design, and asserting the index definition alone would pass against a
        predicate that happened to be inverted.
        """
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        source_id = (
            await conn.execute(text("SELECT id FROM sources WHERE org_id = :o"), {"o": org_a})
        ).scalar_one()
        original = (
            await conn.execute(text("SELECT id FROM documents WHERE org_id = :o"), {"o": org_a})
        ).scalar_one()
        external_id = (
            await conn.execute(
                text("SELECT external_id FROM documents WHERE id = :i"), {"i": original}
            )
        ).scalar_one()

        insert = text(
            "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
            "acl_hash, body_original, body_masked, created_at) "
            "VALUES (:id, :org, :src, :ext, 't', 'h', 'a', 'b', 'b', now())"
        )
        params = {"org": org_a, "src": source_id, "ext": external_id}

        # A second CURRENT version is refused.
        with pytest.raises(DBAPIError):
            await conn.execute(insert, {**params, "id": uuid.uuid4()})
        await conn.rollback()

        # Supersede the original, then the same identifier may have a new current row.
        await _scope(conn, org_a)
        replacement = uuid.uuid4()
        await conn.execute(text("SET CONSTRAINTS fk_documents_superseded_by DEFERRED"))
        await conn.execute(
            text("UPDATE documents SET superseded_by = :new WHERE id = :old"),
            {"new": replacement, "old": original},
        )
        await conn.execute(insert, {**params, "id": replacement})

        current = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM documents WHERE external_id = :e "
                    "AND superseded_by IS NULL"
                ),
                {"e": external_id},
            )
        ).scalar_one()
        total = (
            await conn.execute(
                text("SELECT count(*) FROM documents WHERE external_id = :e"), {"e": external_id}
            )
        ).scalar_one()
        await conn.rollback()

        assert current == 1
        assert total == 2, "the superseded version was not kept"

    async def test_the_supersede_key_is_deferrable(self, conn: AsyncConnection) -> None:
        """Superseding needs it, and an immediate constraint makes the design impossible.

        Pointing the old row at the new one references a row that does not exist yet, and
        inserting the new one first puts two current versions in the table at once. The
        deferral is what lets both statements be honest.
        """
        row = (
            await conn.execute(
                text(
                    "SELECT condeferrable, condeferred FROM pg_constraint "
                    "WHERE conname = 'fk_documents_superseded_by'"
                )
            )
        ).one()

        assert row.condeferrable is True, "supersession cannot be expressed without this"
        assert row.condeferred is False, "deferring by default would weaken every other write"

    async def test_document_idempotency_key_is_unique_among_current_versions(
        self, conn: AsyncConnection
    ) -> None:
        """§4.14 — what makes a second `make seed` add zero rows.

        It was a plain unique constraint until migration 0010 made it a **partial** unique
        index over the same columns, `WHERE superseded_by IS NULL`. The property under
        test is unchanged — one document per source identifier — but it now says *one
        **current** document*, which is what lets re-ingested content supersede rather
        than collide.

        The `WHERE` clause is asserted explicitly, and that assertion is the point. Drop
        the predicate and the index still looks right in a schema dump while quietly
        forbidding every version after the first; keep the predicate but lose a column and
        two tenants collide. Both failures are silent, so both are named here.
        """
        definition = (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_documents_current_org_source_external'"
                )
            )
        ).scalar_one()

        assert "UNIQUE" in definition
        for column in ("org_id", "source_id", "external_id"):
            assert column in definition
        assert "WHERE (superseded_by IS NULL)" in definition, (
            "a non-partial index forbids document versioning entirely"
        )

        superseded = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'uq_documents_org_source_external'"
                )
            )
        ).scalar_one()
        assert superseded == 0, "the old non-partial constraint would still forbid versions"
