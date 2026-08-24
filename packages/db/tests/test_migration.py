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

import pytest
from jutsu_db import EMBEDDING_DIM
from sqlalchemy import text
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

    async def test_document_idempotency_key_is_unique(self, conn: AsyncConnection) -> None:
        """§4.14 — this constraint is what makes a second `make seed` add zero rows."""
        definition = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'uq_documents_org_source_external'"
                )
            )
        ).scalar_one()
        assert "UNIQUE" in definition
        for column in ("org_id", "source_id", "external_id"):
            assert column in definition
