"""Extraction against a real database and deliberately misbehaving models.

The quote gate (non-negotiable 2) cannot be proven with a well-behaved model, so the
fakes here fabricate on purpose: quotes that are not in the chunk, chunk indices that
were never sent, types outside the taxonomy. Every fabrication must be discarded and
counted; everything stored must anchor to a real chunk with a verbatim quote.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from jutsu_db.engine import dispose_engine, org_session
from jutsu_worker.extraction import extract_document
from jutsu_worker.runner import process_extraction
from sqlalchemy import text

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"

pytestmark = pytest.mark.usefixtures("worker_database")


def _alembic_config(url: str) -> Config:
    root = Path(__file__).resolve().parents[3] / "packages" / "db"
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src" / "jutsu_db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
async def worker_database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Migrated schema, app-role engine, disposed on BOTH sides — the process-cached
    engine trap, same as test_ingest_pipeline. Inline rather than in a conftest because
    mypy refuses a second module named conftest under apps/."""
    if os.environ.get("JUTSU_DB_REACHABLE") != "1":
        pytest.skip(f"nothing listening at {TEST_DB_ENV}")
    app_url = os.environ[TEST_DB_ENV]
    migration_url = os.environ.get(MIGRATION_DB_ENV, app_url)

    cfg = _alembic_config(migration_url)
    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")

    monkeypatch.setenv("DATABASE_URL", app_url)
    await dispose_engine()
    yield
    await dispose_engine()
    await asyncio.to_thread(command.downgrade, cfg, "base")


CHUNK_TEXT = (
    "After the outage review on 2014-03-11 the team decided to move the ledger to "
    "PostgreSQL. Sarah Chen owns the migration plan."
)


class ScriptedExtractor:
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def complete(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        return self._responses.pop(0)


def claims_json(*claims: dict[str, object]) -> str:
    return json.dumps({"claims": list(claims)})


async def seed_document(org_id: uuid.UUID) -> uuid.UUID:
    """One org, one source, one document, one chunk carrying CHUNK_TEXT."""
    source_id, document_id = uuid.uuid4(), uuid.uuid4()
    async with org_session(org_id) as session:
        await session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, 'extract-test')"), {"id": org_id}
        )
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
                "VALUES (:id, :org, :src, 'x', 'Outage review', 'h', 'a', :body, :body, now())"
            ),
            {"id": document_id, "org": org_id, "src": source_id, "body": CHUNK_TEXT},
        )
        await session.execute(
            text(
                "INSERT INTO chunks (id, document_id, org_id, ordinal, text, "
                "char_start, char_end, token_count) "
                "VALUES (gen_random_uuid(), :doc, :org, 0, :text, 0, :end, 30)"
            ),
            {"doc": document_id, "org": org_id, "text": CHUNK_TEXT, "end": len(CHUNK_TEXT)},
        )
    return document_id


class TestQuoteGate:
    async def test_a_verbatim_claim_is_stored_with_its_evidence_anchor(self) -> None:
        org_id = uuid.uuid4()
        document_id = await seed_document(org_id)
        quote = "the team decided to move the ledger to PostgreSQL"
        model = ScriptedExtractor(
            claims_json(
                {
                    "type": "decision",
                    "chunk": 1,
                    "quote": quote,
                    "summary": "Move the ledger to PostgreSQL",
                    "confidence": 0.9,
                }
            )
        )

        async with org_session(org_id) as session:
            result = await extract_document(
                session, org_id=org_id, document_id=document_id, transport=model
            )

        assert result.stored == 1
        assert result.gated == 0

        async with org_session(org_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT c.claim_type, c.confidence, c.payload_json, ch.text "
                        "FROM extraction_claims c JOIN chunks ch ON ch.id = c.chunk_id"
                    )
                )
            ).one()
        assert row.claim_type == "decision"
        payload = row.payload_json
        # The evidence anchor re-derives: the stored offsets slice the chunk's masked
        # text back to the exact quote. Non-negotiable 1, checked mechanically.
        assert row.text[payload["char_start"] : payload["char_end"]] == quote
        assert payload["quote"] == quote
        assert payload["extractor_version"]
        assert payload["prompt_hash"]

    async def test_a_fabricated_quote_is_discarded_and_counted(self) -> None:
        """The exact §4.2 defect: fluent, plausible, and not in the source."""
        org_id = uuid.uuid4()
        document_id = await seed_document(org_id)
        model = ScriptedExtractor(
            claims_json(
                {
                    "type": "decision",
                    "chunk": 1,
                    "quote": "the team decided to adopt MongoDB",
                    "summary": "Adopt MongoDB",
                    "confidence": 0.95,
                }
            )
        )

        async with org_session(org_id) as session:
            result = await extract_document(
                session, org_id=org_id, document_id=document_id, transport=model
            )

        assert result.stored == 0
        assert result.gated == 1

        async with org_session(org_id) as session:
            count = (
                await session.execute(text("SELECT count(*) FROM extraction_claims"))
            ).scalar_one()
            stats = (
                await session.execute(text("SELECT stats_json FROM extraction_runs"))
            ).scalar_one()
        assert count == 0
        assert stats["claims_gated"] == 1

    async def test_an_invented_chunk_index_and_type_are_discarded(self) -> None:
        org_id = uuid.uuid4()
        document_id = await seed_document(org_id)
        model = ScriptedExtractor(
            claims_json(
                {"type": "decision", "chunk": 7, "quote": "PostgreSQL", "confidence": 0.9},
                {"type": "prophecy", "chunk": 1, "quote": "PostgreSQL", "confidence": 0.9},
            )
        )

        async with org_session(org_id) as session:
            result = await extract_document(
                session, org_id=org_id, document_id=document_id, transport=model
            )

        assert result.stored == 0
        assert result.gated == 2


class TestRunSemantics:
    async def test_reruns_version_rather_than_overwrite(self) -> None:
        """Non-negotiable 4: two executions, two runs, both sets of claims retained."""
        org_id = uuid.uuid4()
        document_id = await seed_document(org_id)
        claim = {
            "type": "person",
            "chunk": 1,
            "quote": "Sarah Chen",
            "name": "Sarah Chen",
            "confidence": 0.8,
        }

        for _ in range(2):
            model = ScriptedExtractor(claims_json(claim))
            async with org_session(org_id) as session:
                await extract_document(
                    session, org_id=org_id, document_id=document_id, transport=model
                )

        async with org_session(org_id) as session:
            runs = (
                await session.execute(text("SELECT count(*) FROM extraction_runs"))
            ).scalar_one()
            claims = (
                await session.execute(text("SELECT count(*) FROM extraction_claims"))
            ).scalar_one()
        assert runs == 2
        assert claims == 2

    async def test_unparseable_output_retries_once_then_records_the_failure(self) -> None:
        org_id = uuid.uuid4()
        document_id = await seed_document(org_id)
        model = ScriptedExtractor("I think the answer is...", "still not json")

        async with org_session(org_id) as session:
            result = await extract_document(
                session, org_id=org_id, document_id=document_id, transport=model
            )

        assert model.calls == 2
        assert result.stored == 0
        async with org_session(org_id) as session:
            stats = (
                await session.execute(text("SELECT stats_json FROM extraction_runs"))
            ).scalar_one()
        assert stats["parse_failed"] is True


class TestQueueIntegration:
    async def test_a_queued_extraction_runs_and_completes(self) -> None:
        org_id = uuid.uuid4()
        document_id = await seed_document(org_id)
        job_id = uuid.uuid4()
        async with org_session(org_id) as session:
            await session.execute(
                text(
                    "INSERT INTO jobs (id, org_id, kind, state, idempotency_key, payload_json) "
                    "VALUES (:id, :org, 'extract.document', 'pending', :key, "
                    "cast(:payload AS jsonb))"
                ),
                {
                    "id": job_id,
                    "org": str(org_id),
                    "key": f"extract.document:{org_id}:{document_id}",
                    "payload": f'{{"document_id": "{document_id}"}}',
                },
            )

        model = ScriptedExtractor(
            claims_json({"type": "person", "chunk": 1, "quote": "Sarah Chen", "confidence": 0.8})
        )
        outcome = await process_extraction(org_id, job_id=job_id, transport=model)
        assert outcome == 1

        async with org_session(org_id) as session:
            state = (
                await session.execute(text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id})
            ).scalar_one()
        assert state == "completed"
