"""The database-backed clauses, against a real Postgres, plus sabotage.

Everything here runs through `org_session` as the restricted `jutsu_app` role, because
that is what the gate does and the point is to measure the database the application can
actually see. A test that set up its rows through the migration role would be proving a
property of a connection nobody uses (ADR 0003).

The documents are built by the **real** pipeline steps — `mask` with the document id as
namespace, then `chunk_document` — so the offsets under test are the offsets
`persist_document` would have stored. No corpus is required and none is invented: three
documents with deliberately awkward text is enough to exercise the arithmetic, and the
count clause correctly reports a *measured failure* at that scale rather than pretending.

Each sabotage block breaks exactly one property and asserts the matching check goes red.
Without them, a check that always returns "0 mismatches" would look identical to a
correct one, and its entire symptom would be silence.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from jutsu_core.chunking import chunk_document
from jutsu_core.models import content_hash_of
from jutsu_core.pii import mask
from jutsu_db.engine import dispose_engine, org_session
from jutsu_evals.gate import GateContext, Outcome
from jutsu_evals.phase1 import (
    check_chunks_embedded,
    check_documents_ingested,
    check_offsets_resolve,
    check_seed_idempotent,
)
from sqlalchemy import text

DIM = 768
TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"

BODIES = (
    "Raptor review Tuesday. Reach me at k.lay@enron.example or +1 713 555 0142 "
    "if the structure needs another look before we sign anything.",
    "प्रोजेक्ट की समीक्षा सोमवार को है। Contact ops@enron.example for the deck, "
    "and note that 👨‍👩‍👧‍👦 renders as one grapheme cluster in the body text.",
    "Decision: we standardise on PostgreSQL for the primary store. Alternatives "
    "considered were MongoDB and DynamoDB. Owner is ap@enron.example. " * 30,
)


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


def literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


@pytest.fixture
async def seeded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[uuid.UUID]:
    """A migrated test database holding three real documents, org-scoped throughout.

    `DATABASE_URL` is repointed and `dispose_engine()` called on both sides. That is the
    recorded trap: `org_session` caches one engine per process, so without disposing, the
    suite runs against a pool bound to a schema that no longer exists and passes one test
    at a time while failing as a file.
    """
    skip_without_database()
    app_url = os.environ[TEST_DB_ENV]
    migration_url = os.environ.get(MIGRATION_DB_ENV, app_url)

    cfg = alembic_config(migration_url)
    await run_alembic(cfg, "downgrade", "base")
    await run_alembic(cfg, "upgrade", "head")

    monkeypatch.setenv("DATABASE_URL", app_url)
    await dispose_engine()

    org_id = uuid.uuid4()
    source_id = uuid.uuid4()

    async with org_session(org_id) as session:
        await session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i, :n)"), {"i": org_id, "n": "gate"}
        )
        await session.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json) "
                "VALUES (:i, :o, 'local', CAST(:c AS jsonb))"
            ),
            {"i": source_id, "o": org_id, "c": f'{{"root": "{tmp_path.as_posix()}"}}'},
        )

        for index, body in enumerate(BODIES):
            document_id = uuid.uuid4()
            masked = mask(body, namespace=str(document_id))
            await session.execute(
                text(
                    "INSERT INTO documents (id, org_id, source_id, external_id, title, mime, "
                    "content_hash, acl_hash, body_original, body_masked, created_at) "
                    "VALUES (:i, :o, :s, :e, :t, 'text/plain', :ch, :ah, :orig, :masked, :c)"
                ),
                {
                    "i": document_id,
                    "o": org_id,
                    "s": source_id,
                    "e": f"doc-{index}",
                    "t": f"document {index}",
                    "ch": content_hash_of(body),
                    "ah": content_hash_of(f"acl-{index}"),
                    "orig": body,
                    "masked": masked.masked_text,
                    "c": datetime.now(UTC),
                },
            )
            for chunk in chunk_document(masked):
                await session.execute(
                    text(
                        "INSERT INTO chunks (id, document_id, org_id, ordinal, text, "
                        "char_start, char_end, token_count, embedding) "
                        "VALUES (:i, :d, :o, :n, :x, :s, :e, :tk, CAST(:v AS vector))"
                    ),
                    {
                        "i": uuid.uuid4(),
                        "d": document_id,
                        "o": org_id,
                        "n": chunk.ordinal,
                        "x": chunk.text,
                        "s": chunk.char_start,
                        "e": chunk.char_end,
                        "tk": chunk.token_count,
                        "v": literal([0.001 * (chunk.ordinal + 1)] + [0.0] * (DIM - 1)),
                    },
                )

    yield org_id

    await dispose_engine()
    await run_alembic(cfg, "downgrade", "base")


def _ctx(org_id: uuid.UUID, tmp_path: Path, **overrides: object) -> GateContext:
    return GateContext(repo_root=tmp_path, org_id=org_id, **overrides)  # type: ignore[arg-type]


class TestTheOffsetClause:
    async def test_real_pipeline_offsets_resolve(self, seeded: uuid.UUID, tmp_path: Path) -> None:
        result = await check_offsets_resolve(_ctx(seeded, tmp_path, sample=None))
        assert result.outcome is Outcome.PASSED
        assert result.observed == 0

    async def test_sabotaging_one_offset_turns_the_check_red(
        self, seeded: uuid.UUID, tmp_path: Path
    ) -> None:
        """One character. That is the whole defect, and it must be enough."""
        async with org_session(seeded) as session:
            await session.execute(
                text(
                    "UPDATE chunks SET char_start = char_start + 1 WHERE id = "
                    "(SELECT id FROM chunks WHERE char_start > 0 ORDER BY id LIMIT 1)"
                )
            )

        result = await check_offsets_resolve(_ctx(seeded, tmp_path, sample=None))
        assert result.outcome is Outcome.FAILED
        assert result.observed == 1

    async def test_sabotaging_the_stored_text_turns_the_check_red(
        self, seeded: uuid.UUID, tmp_path: Path
    ) -> None:
        """Chunk text that no longer corresponds to its own span."""
        async with org_session(seeded) as session:
            await session.execute(
                text(
                    "UPDATE chunks SET text = text || ' tampered' WHERE id = "
                    "(SELECT id FROM chunks ORDER BY id LIMIT 1)"
                )
            )

        result = await check_offsets_resolve(_ctx(seeded, tmp_path, sample=None))
        assert result.outcome is Outcome.FAILED

    async def test_the_sample_is_deterministic(self, seeded: uuid.UUID, tmp_path: Path) -> None:
        first = await check_offsets_resolve(_ctx(seeded, tmp_path, sample=2))
        second = await check_offsets_resolve(_ctx(seeded, tmp_path, sample=2))
        assert first.detail == second.detail


class TestTheEmbeddingClause:
    async def test_fully_embedded_at_the_spec_width_passes(
        self, seeded: uuid.UUID, tmp_path: Path
    ) -> None:
        result = await check_chunks_embedded(_ctx(seeded, tmp_path))
        assert result.outcome is Outcome.PASSED
        assert result.observed == 0

    async def test_one_missing_vector_turns_the_check_red(
        self, seeded: uuid.UUID, tmp_path: Path
    ) -> None:
        async with org_session(seeded) as session:
            await session.execute(
                text(
                    "UPDATE chunks SET embedding = NULL WHERE id = "
                    "(SELECT id FROM chunks ORDER BY id LIMIT 1)"
                )
            )

        result = await check_chunks_embedded(_ctx(seeded, tmp_path))
        assert result.outcome is Outcome.FAILED
        assert result.observed == 1


class TestTheCountClause:
    async def test_a_small_corpus_is_a_measured_failure_not_an_absence(
        self, seeded: uuid.UUID, tmp_path: Path
    ) -> None:
        """Three documents is a real observation, and it is below the floor.

        This is the distinction the harness turns on: with a database and an org, the
        clause *was* measured, so the answer is `failed` with the number attached — not
        `not_measured`, which is reserved for the case where nothing was looked at.
        """
        result = await check_documents_ingested(_ctx(seeded, tmp_path))
        assert result.outcome is Outcome.FAILED
        assert result.observed == len(BODIES)
        assert result.threshold == 45_000


class TestTenantScoping:
    async def test_another_organisation_sees_none_of_it(
        self, seeded: uuid.UUID, tmp_path: Path
    ) -> None:
        """The gate reads through RLS, so a different scope measures a different world."""
        other = uuid.uuid4()
        async with org_session(other) as session:
            await session.execute(
                text("INSERT INTO orgs (id, name) VALUES (:i, :n)"), {"i": other, "n": "other"}
            )

        documents = await check_documents_ingested(_ctx(other, tmp_path))
        assert documents.observed == 0

        chunks = await check_chunks_embedded(_ctx(other, tmp_path))
        assert chunks.outcome is Outcome.NOT_MEASURED
        assert "no current-version chunks" in chunks.detail


class TestTheIdempotencyClause:
    async def test_a_reseed_that_writes_nothing_passes(
        self, seeded: uuid.UUID, tmp_path: Path
    ) -> None:
        calls: list[str] = []

        async def reseed(org_id: uuid.UUID, root: str) -> int:
            calls.append(root)
            return 0

        result = await check_seed_idempotent(
            _ctx(seeded, tmp_path, allow_writes=True, reseed=reseed)
        )
        assert result.outcome is Outcome.PASSED
        assert result.observed == 0
        assert calls, "the check never re-ran the source"

    async def test_a_reseed_that_writes_a_row_fails(
        self, seeded: uuid.UUID, tmp_path: Path
    ) -> None:
        """Sabotage for the clause whose whole content is "nothing changed"."""

        async def reseed(org_id: uuid.UUID, root: str) -> int:
            async with org_session(org_id) as session:
                document_id = (
                    await session.execute(text("SELECT id FROM documents LIMIT 1"))
                ).scalar_one()
                await session.execute(
                    text(
                        "INSERT INTO chunks (id, document_id, org_id, ordinal, text, "
                        "char_start, char_end, token_count) "
                        "VALUES (:i, :d, :o, 999, 'spurious', 0, 1, 1)"
                    ),
                    {"i": uuid.uuid4(), "d": document_id, "o": org_id},
                )
            return 1

        result = await check_seed_idempotent(
            _ctx(seeded, tmp_path, allow_writes=True, reseed=reseed)
        )
        assert result.outcome is Outcome.FAILED
        assert result.observed == 1

    async def test_it_still_refuses_without_permission_even_with_a_database(
        self, seeded: uuid.UUID, tmp_path: Path
    ) -> None:
        async def reseed(org_id: uuid.UUID, root: str) -> int:  # pragma: no cover - not called
            raise AssertionError("wrote without --allow-writes")

        result = await check_seed_idempotent(_ctx(seeded, tmp_path, reseed=reseed))
        assert result.outcome is Outcome.NOT_MEASURED
