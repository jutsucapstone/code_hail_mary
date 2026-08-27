"""Vectors into `chunks.embedding`, against a real Postgres (spec §8, §4.5, §4.7).

No stand-in. `vector(768)`, the HNSW index and the row-level security policies have no
in-memory equivalent, and a fake would pass while proving nothing about the three things
that matter here: that a 768-vector round-trips, that a mis-scoped write reaches nothing,
and that re-running the pass is a no-op.

**No provider call.** The vectors come from the recorded fixture, so this suite exercises
the database half at full fidelity and costs nothing.

Fixtures live in this module rather than a `conftest.py` for the reason the Makefile
records: `packages/db/tests/conftest.py` already exists, and a second `conftest` under the
`packages` tree makes `mypy packages` ambiguous and check nothing at all.
"""

from __future__ import annotations

import asyncio
import math
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from jutsu_db.engine import dispose_engine, org_session
from jutsu_db.models import Chunk as ChunkRow
from jutsu_retrieval.embeddings import Embedder
from jutsu_retrieval.persistence import (
    embed_pending_chunks,
    pending_chunks,
    store_embeddings,
)
from retrieval_support import FakeTransport, recorded_vector, settings
from sqlalchemy import select, text

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"


def skip_without_database() -> None:
    if os.environ.get("JUTSU_DB_REACHABLE") != "1":
        pytest.skip(f"nothing listening at {TEST_DB_ENV} — start Postgres with `make up`")


def alembic_config(url: str) -> Config:
    root = Path(__file__).resolve().parents[3] / "packages" / "db"
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src" / "jutsu_db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def run_alembic(cfg: Config, direction: str, revision: str) -> None:
    """Alembic's env.py ends in `asyncio.run`, which cannot nest in a running loop."""
    fn = command.upgrade if direction == "upgrade" else command.downgrade
    await asyncio.to_thread(fn, cfg, revision)


@pytest.fixture(name="seeded")
async def seeded_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[uuid.UUID, ...]]:
    """Two organisations, each with one document and three unembedded chunks.

    Two on purpose. An isolation assertion against a database containing one tenant
    passes whatever the code does.
    """
    skip_without_database()
    app_url = os.environ[TEST_DB_ENV]
    migration_url = os.environ.get(MIGRATION_DB_ENV, app_url)

    cfg = alembic_config(migration_url)
    await run_alembic(cfg, "downgrade", "base")
    await run_alembic(cfg, "upgrade", "head")

    # `org_session` reads DATABASE_URL, and it must be the RESTRICTED role — the owner is
    # a superuser and bypasses row-level security unconditionally (ADR 0003), which would
    # make every isolation assertion below vacuous.
    monkeypatch.setenv("DATABASE_URL", app_url)
    await dispose_engine()

    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    for org_id, label in ((org_a, "alpha"), (org_b, "beta")):
        async with org_session(org_id) as session:
            await session.execute(
                text("INSERT INTO orgs (id, name) VALUES (:id, :name)"),
                {"id": org_id, "name": label},
            )
            source_id, doc_id = uuid.uuid4(), uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO sources (id, org_id, system, config_json) "
                    "VALUES (:id, :org, 'local', '{}'::jsonb)"
                ),
                {"id": source_id, "org": org_id},
            )
            await session.execute(
                text(
                    "INSERT INTO documents (id, org_id, source_id, external_id, title, "
                    "content_hash, acl_hash, body_original, body_masked, created_at) "
                    "VALUES (:id, :org, :src, :ext, :title, 'h', 'a', 'body', 'body', now())"
                ),
                {"id": doc_id, "org": org_id, "src": source_id, "ext": label, "title": label},
            )
            for ordinal in range(3):
                await session.execute(
                    text(
                        "INSERT INTO chunks (id, document_id, org_id, ordinal, text, "
                        "char_start, char_end, token_count) "
                        "VALUES (:id, :doc, :org, :ord, :body, 0, 10, 1)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "doc": doc_id,
                        "org": org_id,
                        "ord": ordinal,
                        "body": f"{label} chunk {ordinal}",
                    },
                )

    yield org_a, org_b

    await dispose_engine()
    await run_alembic(cfg, "downgrade", "base")


async def stored_vector(org_id: uuid.UUID, chunk_id: uuid.UUID) -> list[float] | None:
    """Read a vector back through the typed column.

    Through `ChunkRow.embedding` rather than a raw `text()` SELECT, deliberately: raw SQL
    hands back pgvector's literal text form (`'[0.1,0.2,...]'`) because no type is
    attached to the result, so a test reading that way would be asserting about a string.
    Going through the mapped column exercises the same type coercion S7 will use.
    """
    async with org_session(org_id) as session:
        row = (
            await session.execute(select(ChunkRow.embedding).where(ChunkRow.id == chunk_id))
        ).first()
    if row is None or row[0] is None:
        return None
    return [float(value) for value in row[0]]


def norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


class TestVectorRoundTrip:
    async def test_a_768_vector_survives_the_round_trip(
        self, seeded: tuple[uuid.UUID, ...]
    ) -> None:
        org_a, _ = seeded
        pending = await pending_chunks(org_a, limit=10)
        assert len(pending) == 3

        vector = list(recorded_vector(0))
        assert len(vector) == 768

        written = await store_embeddings(org_a, [pending[0].chunk_id], [vector], [42])
        assert written == 1

        stored = await stored_vector(org_a, pending[0].chunk_id)
        assert stored is not None
        assert len(stored) == 768
        for original, roundtripped in zip(vector, stored, strict=True):
            assert roundtripped == pytest.approx(original, abs=1e-6)

    async def test_the_provider_token_count_replaces_the_estimate(
        self, seeded: tuple[uuid.UUID, ...]
    ) -> None:
        """ADR 0006 shipped S5 with an estimate in this column and said the real number
        arrives here. It does."""
        org_a, _ = seeded
        pending = await pending_chunks(org_a, limit=1)
        await store_embeddings(org_a, [pending[0].chunk_id], [recorded_vector(0)], [4242])

        async with org_session(org_a) as session:
            row = (
                await session.execute(
                    text("SELECT token_count FROM chunks WHERE id = :id"),
                    {"id": pending[0].chunk_id},
                )
            ).one()
        assert row.token_count == 4242

    async def test_a_normalised_vector_is_stored_at_unit_length(
        self, seeded: tuple[uuid.UUID, ...]
    ) -> None:
        org_a, _ = seeded
        embedder = Embedder(FakeTransport(), settings(), count_tokens=len)
        run = await embed_pending_chunks(org_a, embedder, limit=10)
        assert run.embedded == 3

        for chunk in await all_chunk_ids(org_a):
            stored = await stored_vector(org_a, chunk)
            assert stored is not None
            assert norm(stored) == pytest.approx(1.0, abs=1e-5)


async def all_chunk_ids(org_id: uuid.UUID) -> list[uuid.UUID]:
    async with org_session(org_id) as session:
        rows = await session.execute(text("SELECT id FROM chunks ORDER BY id"))
        return [row.id for row in rows]


class TestCrossTenantIsolation:
    async def test_pending_chunks_sees_only_its_own_organisation(
        self, seeded: tuple[uuid.UUID, ...]
    ) -> None:
        org_a, org_b = seeded
        a_texts = {chunk.body for chunk in await pending_chunks(org_a, limit=50)}
        b_texts = {chunk.body for chunk in await pending_chunks(org_b, limit=50)}

        assert all(body.startswith("alpha") for body in a_texts)
        assert all(body.startswith("beta") for body in b_texts)
        assert a_texts.isdisjoint(b_texts)

    async def test_a_write_scoped_to_the_wrong_org_reaches_nothing(
        self, seeded: tuple[uuid.UUID, ...]
    ) -> None:
        """Adversarial: org B holds org A's chunk id and tries to write to it.

        RLS makes the row invisible, so the UPDATE matches nothing. Zero rows affected is
        the correct failure — it is a refusal, not a partial success.
        """
        org_a, org_b = seeded
        target = (await pending_chunks(org_a, limit=1))[0]

        written = await store_embeddings(org_b, [target.chunk_id], [recorded_vector(0)], [7])
        assert written == 0
        assert await stored_vector(org_a, target.chunk_id) is None

    async def test_embedding_one_org_leaves_the_other_untouched(
        self, seeded: tuple[uuid.UUID, ...]
    ) -> None:
        org_a, org_b = seeded
        embedder = Embedder(FakeTransport(), settings(), count_tokens=len)
        await embed_pending_chunks(org_a, embedder, limit=50)

        assert len(await pending_chunks(org_a, limit=50)) == 0
        assert len(await pending_chunks(org_b, limit=50)) == 3


class TestIdempotencyAndResume:
    async def test_a_second_pass_does_nothing(self, seeded: tuple[uuid.UUID, ...]) -> None:
        """§4.14. Re-running after a full success embeds nothing and spends nothing."""
        org_a, _ = seeded
        embedder = Embedder(FakeTransport(), settings(), count_tokens=len)

        first = await embed_pending_chunks(org_a, embedder, limit=50)
        assert first.embedded == 3
        assert first.requests >= 1

        second = await embed_pending_chunks(org_a, embedder, limit=50)
        assert second.embedded == 0
        assert second.tokens == 0
        assert second.requests == 0

    async def test_a_partial_pass_resumes_where_it_stopped(
        self, seeded: tuple[uuid.UUID, ...]
    ) -> None:
        """Resumability is the selection, not a checkpoint — there is no cursor to corrupt."""
        org_a, _ = seeded
        embedder = Embedder(FakeTransport(), settings(), count_tokens=len)

        first = await embed_pending_chunks(org_a, embedder, limit=1)
        assert first.embedded == 1
        assert len(await pending_chunks(org_a, limit=50)) == 2

        second = await embed_pending_chunks(org_a, embedder, limit=50)
        assert second.embedded == 2
        assert len(await pending_chunks(org_a, limit=50)) == 0

    async def test_a_failed_pass_writes_nothing_and_leaves_work_pending(
        self, seeded: tuple[uuid.UUID, ...]
    ) -> None:
        """The chunks stay NULL, so the next run picks them up. No half-written pass."""
        from jutsu_retrieval.errors import PermanentEmbeddingError

        org_a, _ = seeded
        broken = FakeTransport(script=[PermanentEmbeddingError("rejected", status=400)])
        embedder = Embedder(broken, settings(), count_tokens=len)

        with pytest.raises(PermanentEmbeddingError):
            await embed_pending_chunks(org_a, embedder, limit=50)

        assert len(await pending_chunks(org_a, limit=50)) == 3

    async def test_nothing_pending_is_not_an_error(self, seeded: tuple[uuid.UUID, ...]) -> None:
        org_a, _ = seeded
        embedder = Embedder(FakeTransport(), settings(), count_tokens=len)
        await embed_pending_chunks(org_a, embedder, limit=50)

        run = await embed_pending_chunks(org_a, embedder, limit=50)
        assert (run.embedded, run.tokens, run.requests) == (0, 0, 0)


class TestNoLeakageInLogs:
    async def test_the_pass_logs_counts_not_content(
        self, seeded: tuple[uuid.UUID, ...], caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        org_a, _ = seeded
        embedder = Embedder(FakeTransport(), settings(), count_tokens=len)

        with caplog.at_level(logging.DEBUG, logger="jutsu"):
            await embed_pending_chunks(org_a, embedder, limit=50)

        assert caplog.records, "the pass should record that it ran"
        for record in caplog.records:
            message = record.getMessage()
            assert "alpha chunk" not in message, "chunk text reached a log record"
            assert "embedded=" in message or "tokens=" in message or "requests=" in message
