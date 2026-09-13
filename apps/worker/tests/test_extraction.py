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
from jutsu_llm import AllProvidersFailed, LLMRequest, LLMResponse
from jutsu_worker.extraction import EXTRACTION_MAX_TOKENS, UNRESOLVED, extract_document
from jutsu_worker.runner import JOB_FAILED, process_extraction
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


#: What the scripted transports below claim answered. Two distinct vendors, because the
#: provenance assertions have to be able to tell the first call from the second — a
#: retry may well be served by a different provider than the call it is retrying, and
#: the claims that get stored are the retry's.
FIRST_PROVIDER = "cerebras"
FIRST_MODEL = "gpt-oss-120b"
SECOND_PROVIDER = "openrouter"
SECOND_MODEL = "openai/gpt-oss-120b"


class ScriptedExtractor:
    """The chain's `generate`, scripted, and recording what it was asked for.

    `generate` rather than `complete`: extraction reads `provider` and `model` off the
    response and writes them into every claim, so a fake returning a bare string could
    not exercise the provenance the run is judged on.
    """

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.requests.append(request)
        # The second answer comes from the second vendor, as a real fallback would.
        first = self.calls == 1
        return LLMResponse(
            content=self._responses.pop(0),
            provider=FIRST_PROVIDER if first else SECOND_PROVIDER,
            model=FIRST_MODEL if first else SECOND_MODEL,
            latency_ms=7,
        )


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
        # Provenance names the model that ACTUALLY answered, not a configured default.
        # With a fallback chain those differ whenever the primary is unwell, and
        # non-negotiable 1 asks for the real one.
        assert payload["model"] == FIRST_MODEL
        assert payload["provider"] == FIRST_PROVIDER

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


class TestTheProvenanceNamesWhatAnswered:
    """Which model produced a claim, recorded from the response rather than from config.

    This is the half of ADR 0024 that extraction does not share with the answer path.
    `/v1/ask` composes a prompt, reads `.content` and throws the rest away; extraction
    *persists* what it was told, and non-negotiable 1 requires `model` on every piece of
    evidence. Reading it from an environment variable was correct when there was one
    vendor and one model id; with a chain it names the model JUTSU hoped would answer,
    which on the day a fallback fires is a different model from the one that did.
    """

    async def test_the_run_and_its_claims_name_the_model_that_answered(self) -> None:
        org_id = uuid.uuid4()
        document_id = await seed_document(org_id)
        model = ScriptedExtractor(
            claims_json(
                {
                    "type": "decision",
                    "chunk": 1,
                    "quote": "the team decided to move the ledger to PostgreSQL",
                    "summary": "Move the ledger",
                    "confidence": 0.9,
                }
            )
        )

        async with org_session(org_id) as session:
            await extract_document(session, org_id=org_id, document_id=document_id, transport=model)

        async with org_session(org_id) as session:
            run = (
                await session.execute(text("SELECT model, stats_json FROM extraction_runs"))
            ).one()
            payload = (
                await session.execute(text("SELECT payload_json FROM extraction_claims"))
            ).scalar_one()
        assert run.model == FIRST_MODEL
        assert run.stats_json["provider"] == FIRST_PROVIDER
        assert payload["model"] == FIRST_MODEL
        assert payload["provider"] == FIRST_PROVIDER

    async def test_a_retry_answered_by_a_second_vendor_is_what_gets_recorded(self) -> None:
        """The claims stored are the retry's, so the provenance must be the retry's too.

        A chain can answer the first call from Cerebras and the retry from OpenRouter —
        they are independent attempts — and recording the first attempt's model against
        claims the second one produced would be a false attribution written by the code
        that is supposed to prevent them.
        """
        org_id = uuid.uuid4()
        document_id = await seed_document(org_id)
        model = ScriptedExtractor(
            "not json at all",
            claims_json(
                {
                    "type": "decision",
                    "chunk": 1,
                    "quote": "the team decided to move the ledger to PostgreSQL",
                    "summary": "Move the ledger",
                    "confidence": 0.9,
                }
            ),
        )

        async with org_session(org_id) as session:
            result = await extract_document(
                session, org_id=org_id, document_id=document_id, transport=model
            )

        assert model.calls == 2
        assert result.stored == 1
        async with org_session(org_id) as session:
            run = (
                await session.execute(text("SELECT model, stats_json FROM extraction_runs"))
            ).one()
            payload = (
                await session.execute(text("SELECT payload_json FROM extraction_claims"))
            ).scalar_one()
        assert run.model == SECOND_MODEL
        assert run.stats_json["provider"] == SECOND_PROVIDER
        assert payload["model"] == SECOND_MODEL
        assert payload["provider"] == SECOND_PROVIDER

    async def test_a_document_with_nothing_to_read_names_no_model(self) -> None:
        """No call, so no model — and the row says so rather than naming a plausible one.

        `extraction_runs.model` is NOT NULL, so the row has to say something. A model id
        would be provenance for a call that never happened.
        """
        org_id = uuid.uuid4()
        document_id = await seed_document(org_id)
        async with org_session(org_id) as session:
            await session.execute(
                text("DELETE FROM chunks WHERE document_id = :doc"), {"doc": document_id}
            )
        model = ScriptedExtractor()

        async with org_session(org_id) as session:
            result = await extract_document(
                session, org_id=org_id, document_id=document_id, transport=model
            )

        assert model.calls == 0
        assert result.chunks_total == 0
        async with org_session(org_id) as session:
            run = (
                await session.execute(text("SELECT model, stats_json FROM extraction_runs"))
            ).one()
        assert run.model == UNRESOLVED
        assert run.stats_json["provider"] == UNRESOLVED

    async def test_extraction_asks_for_its_own_token_ceiling_not_the_answer_paths(self) -> None:
        """A document's worth of claims does not fit in a paragraph's worth of tokens.

        Carried on the request rather than configured inside the chain, so the chain does
        not have to know which caller it is serving — and so a truncated extraction is a
        number in this file rather than a silent shortfall in a nightly job.
        """
        org_id = uuid.uuid4()
        document_id = await seed_document(org_id)
        model = ScriptedExtractor(claims_json())

        async with org_session(org_id) as session:
            await extract_document(session, org_id=org_id, document_id=document_id, transport=model)

        assert [request.max_tokens for request in model.requests] == [EXTRACTION_MAX_TOKENS]


class TestQueueIntegration:
    async def test_a_rate_limited_model_lands_retry_scheduled_with_its_kind(self) -> None:
        """The work transaction dies with the provider error inside it; the classified
        failure must still land on the row — runner discipline, a NEW transaction —
        or the job sits in a working state until its lease expires, kindless, and the
        429 reads as a crash instead of a provider saying "not now"."""
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

        class RateLimited:
            """Every vendor over capacity — what the chain raises once it has run out."""

            async def generate(self, request: LLMRequest) -> LLMResponse:
                raise AllProvidersFailed(
                    "The answer service is briefly over capacity. Try again shortly.",
                    error_class="rate_limited",
                )

        outcome = await process_extraction(org_id, job_id=job_id, transport=RateLimited())
        assert outcome is JOB_FAILED

        async with org_session(org_id) as session:
            job = (
                await session.execute(
                    text("SELECT state, failure_kind, attempts FROM jobs WHERE id = :id"),
                    {"id": job_id},
                )
            ).one()
            runs = (
                await session.execute(text("SELECT count(*) FROM extraction_runs"))
            ).scalar_one()
        assert job.state == "retry_scheduled"
        assert job.failure_kind == "provider_transient"
        assert job.attempts == 1, "the claim's increment survived the failure"
        assert runs == 0, "the work transaction rolled back; the failure write did not ride it"

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
