"""The ingestion pipeline end to end, against real Postgres and real pgvector.

    fixture corpus -> connector -> ingest.source -> ingest.document
                   -> PII mask -> chunk -> embed.document -> pgvector
                   -> S7 search_chunks -> authorized evidence

The closing test walks that whole chain and then asks S7 for the result, because every
stage passing in isolation still permits a pipeline that stores something retrieval cannot
see. Nothing between the corpus and the vector is stubbed except the embedding provider,
which uses a deterministic fake transport — a real Gemini call belongs behind
`JUTSU_LIVE_EMBEDDING_SMOKE`, and a corpus-wide embedding job is not something a test
suite should ever start.

**No mocked database anywhere.** Idempotency is a unique constraint, tenant isolation is a
policy, versioning is a partial index and crash recovery is a lease. All four are the
server's behaviour, and a fake session would pass against an implementation that has none
of them.

Mail fixtures are built in code with explicit CRLF, never committed as files:
`.gitattributes` is `* text=auto eol=lf` and a MIME boundary is defined in terms of CRLF,
so a checked-in fixture parses differently from the mail it imitates (ADR 0008).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from jutsu_db.engine import dispose_engine
from jutsu_retrieval.config import EmbeddingSettings
from jutsu_retrieval.embeddings import Embedder
from jutsu_retrieval.errors import (
    PermanentEmbeddingError,
    TransientEmbeddingError,
)
from jutsu_retrieval.search import search_chunks
from jutsu_worker.cli import seed
from jutsu_worker.ingest import (
    run_document_job,
    run_embedding_job,
    run_source_job,
)
from jutsu_worker.jobs import (
    FailureKind,
    JobKind,
    JobState,
    claim_job,
)
from jutsu_worker.pipeline import IngestOutcome
from jutsu_worker.runner import (
    process_document,
    process_embedding,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

CRLF = "\r\n"
DIM = 768

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"


# --------------------------------------------------------------------------------------
# Infrastructure fixtures
# --------------------------------------------------------------------------------------


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
    fn = command.upgrade if direction == "upgrade" else command.downgrade
    await asyncio.to_thread(fn, cfg, revision)


async def clear_tenant_data(url: str) -> None:
    """Remove every tenant row, if the tables are there at all.

    Called on **both** sides of the fixture, and the setup call is the one that matters:
    migration 0010's downgrade refuses to run while any document has been superseded —
    deliberately, because restoring the old non-partial constraint over real version
    history would mean destroying it. So a test that creates versions leaves a schema that
    cannot be torn down, and without this every later test fails in *setup* with an error
    about a constraint it never touched.

    Guarded on existence so it is a no-op against a database that is already at base.
    """
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        present = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = 'documents'"
                )
            )
        ).scalar_one()
        if present:
            await connection.execute(
                text("TRUNCATE documents, sources, jobs, audit_log, orgs CASCADE")
            )
    await engine.dispose()


@pytest.fixture
async def session(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncSession]:
    """One transaction as the restricted `jutsu_app` role.

    `DATABASE_URL` is pointed at that same restricted role, because `embed_pending_chunks`
    opens its **own** session through `org_session` and pointing it at the owner would make
    the isolation assertions in this file vacuous (ADR 0003).
    """
    skip_without_database()
    app_url = os.environ[TEST_DB_ENV]
    migration_url = os.environ.get(MIGRATION_DB_ENV, app_url)

    monkeypatch.setenv("DATABASE_URL", migration_url)
    cfg = alembic_config(migration_url)
    await clear_tenant_data(migration_url)
    await run_alembic(cfg, "downgrade", "base")
    await run_alembic(cfg, "upgrade", "head")

    # `org_session` caches one engine for the whole process, so a test that ran earlier
    # leaves a pool bound to a schema this fixture has just dropped and rebuilt. Disposing
    # it on both sides is what keeps the tests independent — without it the suite passes
    # one test at a time and fails as a file, which is a fixture bug wearing the costume
    # of a product bug.
    monkeypatch.setenv("DATABASE_URL", app_url)
    await dispose_engine()

    engine = create_async_engine(app_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    # No `begin()` context manager: the pipeline's real shape is several transactions in
    # sequence — claim, work, embed — so a test that could not commit could only exercise
    # a shape production never runs.
    async with factory() as opened:
        yield opened
        await opened.rollback()

    await engine.dispose()
    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", migration_url)

    # Clear the tenant data before downgrading, and the reason is a *feature* rather than
    # a workaround. Migration 0010's downgrade restores the old non-partial unique
    # constraint, which cannot represent version history, so it refuses to run while any
    # document has been superseded — correctly, because the alternative is a migration
    # that silently deletes a tenant's document versions to make a constraint fit. A test
    # that creates versions therefore has to remove them itself.
    owner = create_async_engine(migration_url, isolation_level="AUTOCOMMIT")
    async with owner.connect() as connection:
        await connection.execute(text("TRUNCATE documents, sources, jobs, audit_log, orgs CASCADE"))
    await owner.dispose()

    await run_alembic(cfg, "downgrade", "base")


# --------------------------------------------------------------------------------------
# Corpus fixtures — real mail bytes, built here so the ACLs under test are exact
# --------------------------------------------------------------------------------------


def mail(*, sender: str, to: str, subject: str, body: str) -> bytes:
    """One RFC822 message with explicit CRLF.

    The participants matter more than the text: `acls_for` derives the read grants from
    exactly these headers, so `sender` and `to` are what the ACL assertions are about.
    """
    headers = [
        f"Message-ID: <{subject.replace(' ', '-')}@example.com>",
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        "Date: Mon, 3 Mar 2003 09:00:00 -0800",
    ]
    return (CRLF.join(headers) + CRLF + CRLF + body).encode("utf-8")


def write_corpus(root: Path, files: dict[str, bytes]) -> Path:
    for name, payload in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return root


#: A body carrying values every masking detector should catch, so "the original is stored
#: and the masked copy is what travels" can be asserted rather than assumed.
PII_BODY = (
    "Please contact ada@example.com about the account.\n"
    "Card 4111 1111 1111 1111 and phone +1 415 555 0132 are on file.\n"
    "Reference IBAN GB82WEST12345698765432 for the transfer."
)

SECRET_VALUES = ("ada@example.com", "4111 1111 1111 1111", "GB82WEST12345698765432")


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Two documents with different participants, so ACL filtering has something to do."""
    return write_corpus(
        tmp_path / "maildir",
        {
            "grace/1.": mail(
                sender="grace@example.com",
                to="grace@example.com",
                subject="quarterly plan",
                body=PII_BODY,
            ),
            "eve/1.": mail(
                sender="eve@example.com",
                to="eve@example.com",
                subject="private note",
                body="Eve's private note about the acquisition.",
            ),
        },
    )


# --------------------------------------------------------------------------------------
# Embedding fixtures — deterministic, offline
# --------------------------------------------------------------------------------------


@dataclass
class FakeTransport:
    """A scripted `EmbeddingTransport`. Never touches a network.

    `failures` is a queue of exceptions raised before any successful response, which is how
    the transient/permanent classification tests provoke a real failure through the real
    `Embedder` rather than by patching it.
    """

    calls: int = 0
    failures: list[Exception] = field(default_factory=list)

    async def predict(
        self, *, instances: list[dict[str, Any]], parameters: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {
            "predictions": [
                {
                    "embeddings": {
                        # A unit-ish vector that differs per instance, so ordering
                        # mistakes are visible rather than masked by identical vectors.
                        "values": [1.0 if index == position % DIM else 0.0 for index in range(DIM)],
                        "statistics": {"token_count": 8, "truncated": False},
                    }
                }
                for position, _ in enumerate(instances)
            ]
        }


def embedding_settings(**overrides: Any) -> EmbeddingSettings:
    base: dict[str, Any] = {
        "project": "test-project",
        "location": "asia-south1",
        "max_batch_size": 250,
        "max_batch_tokens": 20_000,
        "max_concurrency": 1,
        "base_backoff_s": 0.0,
        "max_backoff_s": 0.0,
    }
    base.update(overrides)
    return EmbeddingSettings(**base)


def embedder(transport: FakeTransport, **overrides: Any) -> Embedder:
    return Embedder(transport, embedding_settings(**overrides))


# --------------------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------------------


async def scope(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
    )


async def make_tenant(session: AsyncSession, label: str, corpus_root: Path) -> dict[str, Any]:
    """An organisation with a local source pointed at a corpus, and one linked user.

    The user holds `local:{label}@example.com`, which is exactly the principal form the
    connector emits from the mail headers — so S7's filter has a real match to find rather
    than a fixture-shaped one.
    """
    org_id, user_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await scope(session, org_id)
    await session.execute(
        text("INSERT INTO orgs (id, name) VALUES (:i, :n)"), {"i": org_id, "n": label}
    )
    await session.execute(
        text("INSERT INTO users (id, org_id, email, status) VALUES (:i,:o,:e,'active')"),
        {"i": user_id, "o": org_id, "e": f"{label}@example.com"},
    )
    await session.execute(
        text(
            "INSERT INTO source_identities (org_id, user_id, source_system, subject) "
            "VALUES (:o,:u,'local',:s)"
        ),
        {"o": org_id, "u": user_id, "s": f"{label}@example.com"},
    )
    await session.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:i,:o,'local',CAST(:c AS jsonb))"
        ),
        {"i": source_id, "o": org_id, "c": f'{{"root": {str(corpus_root)!r}}}'.replace("'", '"')},
    )
    return {"org_id": org_id, "user_id": user_id, "source_id": source_id}


async def walk_source(session: AsyncSession, tenant: dict[str, Any], label: str = "run") -> Any:
    """Run the source walk and commit it, as a worker does.

    The commit is not test scaffolding: the document jobs must be durable before anything
    claims them, and the embedding stage opens its own connection which cannot see an
    uncommitted row.
    """
    run = await run_source_job(
        session,
        org_id=tenant["org_id"],
        source_id=tenant["source_id"],
        correlation_id=label,
    )
    await session.commit()
    # `set_config(..., is_local => true)` is transaction-scoped, so committing drops the
    # org scope and every later statement on this session would be filtered to nothing by
    # the policy. Re-scoping after a commit is not test hygiene — it is the same thing a
    # request or a worker does when it opens its next transaction.
    await scope(session, tenant["org_id"])
    return run


async def drain_documents(tenant: dict[str, Any], limit: int = 50) -> list[IngestOutcome]:
    """Run every queued document job through the real runner. Returns their outcomes."""
    outcomes: list[IngestOutcome] = []
    for _ in range(limit):
        outcome = await process_document(tenant["org_id"])
        if outcome is None:
            break
        outcomes.append(outcome)
    return outcomes


async def drain_embeddings(
    tenant: dict[str, Any], transport: FakeTransport | None = None, limit: int = 50
) -> int:
    """Run every queued embedding job through the real runner. Returns vectors written."""
    written = 0
    worker = embedder(transport or FakeTransport())
    for _ in range(limit):
        count = await process_embedding(tenant["org_id"], worker)
        if count is None:
            break
        written += count
    return written


async def requeue(
    session: AsyncSession, tenant: dict[str, Any], *, kind: JobKind | None = None
) -> None:
    """Put finished jobs back in the queue, as a fresh sync would, and commit.

    Stands in for the source producing the same identifiers again. The commit matters for
    the same reason it does everywhere else here: the runner claims on its own connection
    and cannot see an uncommitted state change.
    """
    if kind is None:
        await session.execute(text("UPDATE jobs SET state = 'pending', locked_until = NULL"))
    else:
        await session.execute(
            text("UPDATE jobs SET state = 'pending', locked_until = NULL WHERE kind = :k"),
            {"k": kind.value},
        )
    await session.commit()
    await scope(session, tenant["org_id"])


async def refresh(session: AsyncSession, tenant: dict[str, Any]) -> None:
    """Drop this session's snapshot so it sees what the runner committed."""
    await session.rollback()
    await scope(session, tenant["org_id"])


async def ingest_everything(
    session: AsyncSession, tenant: dict[str, Any], transport: FakeTransport | None = None
) -> None:
    """The whole pipeline for one tenant, start to finish, through the runner."""
    await walk_source(session, tenant)
    await drain_documents(tenant)
    await drain_embeddings(tenant, transport)
    # The runner committed on its own connections; re-scope this session so assertions
    # made through it see the committed rows.
    await session.rollback()
    await scope(session, tenant["org_id"])


async def counts(session: AsyncSession) -> dict[str, int]:
    return {
        table: (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
        for table in ("documents", "chunks", "document_acl", "jobs")
    }


# --------------------------------------------------------------------------------------


class TestSuccessfulIngestion:
    async def test_the_whole_pipeline_runs(self, session: AsyncSession, corpus: Path) -> None:
        tenant = await make_tenant(session, "grace", corpus)

        run = await walk_source(session, tenant)

        assert run.listed == 2
        assert run.enqueued == 2
        assert run.cursor is not None, "the cursor must advance with the enqueue"

        outcomes = await drain_documents(tenant)
        assert outcomes == [IngestOutcome.CREATED, IngestOutcome.CREATED]

        after = await counts(session)
        assert after["documents"] == 2
        assert after["chunks"] >= 2
        assert after["document_acl"] >= 2

    async def test_embedding_stores_vectors(self, session: AsyncSession, corpus: Path) -> None:
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        pending = (
            await session.execute(text("SELECT count(*) FROM chunks WHERE embedding IS NULL"))
        ).scalar_one()
        stored = (
            await session.execute(text("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL"))
        ).scalar_one()

        assert pending == 0
        assert stored >= 2

    async def test_the_provider_token_count_is_stored(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """S6 overwrites the S5 estimate with what the provider actually billed."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        tokens = (
            (
                await session.execute(
                    text("SELECT DISTINCT token_count FROM chunks WHERE embedding IS NOT NULL")
                )
            )
            .scalars()
            .all()
        )

        assert list(tokens) == [8], "the provider's token count did not reach the row"


class TestIdempotency:
    async def test_a_second_source_run_enqueues_nothing_new(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """§4.14 — the property `make seed` depends on."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)
        before = await counts(session)

        second = await walk_source(session, tenant)

        assert second.enqueued == 0
        assert second.duplicate == second.listed
        assert await counts(session) == before

    async def test_reingesting_unchanged_content_writes_nothing(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """The content hash decides, and an unchanged body costs no writes at all."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)
        before = await counts(session)

        # Force the documents back into the queue as if a new sync had found them.
        await requeue(session, tenant)
        outcomes = await drain_documents(tenant)

        assert outcomes == [IngestOutcome.UNCHANGED, IngestOutcome.UNCHANGED]
        after = await counts(session)
        assert after["documents"] == before["documents"]
        assert after["chunks"] == before["chunks"]

    async def test_an_unchanged_document_queues_no_embedding_work(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """Re-embedding identical content would spend budget for an identical vector."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)
        before_embed_jobs = (
            await session.execute(
                text("SELECT count(*) FROM jobs WHERE kind = :k"),
                {"k": JobKind.EMBED_DOCUMENT.value},
            )
        ).scalar_one()

        await requeue(session, tenant, kind=JobKind.INGEST_DOCUMENT)
        await drain_documents(tenant)

        await refresh(session, tenant)
        queued = (
            await session.execute(
                text("SELECT count(*) FROM jobs WHERE kind = :k"),
                {"k": JobKind.EMBED_DOCUMENT.value},
            )
        ).scalar_one()
        assert before_embed_jobs == 2, "nothing was queued the first time, so this proves nothing"
        assert queued == before_embed_jobs, "an unchanged document queued more embedding work"

    async def test_duplicate_dispatch_of_one_job_runs_once(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """Redis may deliver the same message twice; the lease is what makes that safe."""
        tenant = await make_tenant(session, "grace", corpus)
        await walk_source(session, tenant)
        first = await claim_job(session, kind=JobKind.INGEST_DOCUMENT)
        assert first is not None

        second = await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=first.id)

        assert second is None, "a job under an unexpired lease was claimed twice"


class TestVersioning:
    async def test_changed_content_supersedes_the_old_version(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)
        original = (
            await session.execute(text("SELECT id FROM documents WHERE title = 'quarterly plan'"))
        ).scalar_one()

        (corpus / "grace" / "1.").write_bytes(
            mail(
                sender="grace@example.com",
                to="grace@example.com",
                subject="quarterly plan",
                body="Completely different text for the revised plan.",
            )
        )
        # A real re-sync, not a hand-reset job. The walk reopens the completed job, which
        # is the only thing that makes a changed document reachable at all.
        run = await walk_source(session, tenant, "resync")
        # One file was rewritten, so one file has a newer mtime and one job reopens.
        assert run.reopened == 1, "the walk did not reopen the changed document's job"
        outcomes = await drain_documents(tenant)

        await refresh(session, tenant)
        assert IngestOutcome.UPDATED in outcomes
        rows = (
            await session.execute(
                text(
                    "SELECT id, superseded_by FROM documents WHERE title = 'quarterly plan' "
                    "ORDER BY ingested_at"
                )
            )
        ).all()
        assert len(rows) == 2, "the old version was not preserved"
        assert rows[0].id == original
        assert rows[0].superseded_by == rows[1].id
        assert rows[1].superseded_by is None

    async def test_exactly_one_current_version_exists(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """The partial unique index, seen from the application side."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        for revision in range(3):
            (corpus / "grace" / "1.").write_bytes(
                mail(
                    sender="grace@example.com",
                    to="grace@example.com",
                    subject="quarterly plan",
                    body=f"Revision {revision} of the plan, materially different.",
                )
            )
            await requeue(session, tenant)
            await drain_documents(tenant)

        await refresh(session, tenant)
        current = (
            await session.execute(
                text(
                    "SELECT count(*) FROM documents "
                    "WHERE title = 'quarterly plan' AND superseded_by IS NULL"
                )
            )
        ).scalar_one()
        total = (
            await session.execute(
                text("SELECT count(*) FROM documents WHERE title = 'quarterly plan'")
            )
        ).scalar_one()

        assert current == 1
        assert total == 4, "historical versions were destroyed"

    async def test_a_new_version_gets_its_own_embedding_job(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        (corpus / "grace" / "1.").write_bytes(
            mail(
                sender="grace@example.com",
                to="grace@example.com",
                subject="quarterly plan",
                body="Revised text entirely.",
            )
        )
        await requeue(session, tenant)
        await drain_documents(tenant)

        await refresh(session, tenant)
        queued = (
            await session.execute(
                text("SELECT count(*) FROM jobs WHERE kind = :k"),
                {"k": JobKind.EMBED_DOCUMENT.value},
            )
        ).scalar_one()
        assert queued == 3, "the new version was never queued for embedding"

    async def test_superseded_chunks_are_invisible_to_retrieval(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """S7 already excludes superseded documents; versioning must not defeat that."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        (corpus / "grace" / "1.").write_bytes(
            mail(
                sender="grace@example.com",
                to="grace@example.com",
                subject="quarterly plan",
                body="The superseding revision of the plan.",
            )
        )
        await requeue(session, tenant)
        await drain_documents(tenant)
        await drain_embeddings(tenant)

        page = await search_chunks(
            session, user_id=tenant["user_id"], query_vector=[1.0] + [0.0] * (DIM - 1), k=50
        )
        superseded = (
            (
                await session.execute(
                    text("SELECT id FROM documents WHERE superseded_by IS NOT NULL")
                )
            )
            .scalars()
            .all()
        )

        assert superseded, "nothing was superseded, so this proves nothing"
        assert not (set(superseded) & {item.document_id for item in page.items})


class TestAclAndMasking:
    async def test_source_grants_survive_ingestion(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """The grants are the message's own participants, namespaced (ADR 0008, 0010)."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        grants = (
            (
                await session.execute(
                    text(
                        "SELECT a.principal_id FROM document_acl a JOIN documents d ON d.id = "
                        "a.document_id WHERE d.title = 'quarterly plan'"
                    )
                )
            )
            .scalars()
            .all()
        )

        assert "local:grace@example.com" in grants
        assert all(g.startswith("local:") for g in grants), "a grant lost its namespace"

    async def test_no_grant_is_invented_for_the_other_document(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """Eve's message must not acquire Grace's grant, or anyone else's."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        grants = (
            (
                await session.execute(
                    text(
                        "SELECT a.principal_id FROM document_acl a JOIN documents d ON d.id = "
                        "a.document_id WHERE d.title = 'private note'"
                    )
                )
            )
            .scalars()
            .all()
        )

        assert set(grants) == {"local:eve@example.com"}

    async def test_the_stored_chunk_text_is_masked(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """§9.1 — what the model reads must not carry the raw values."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        chunk_text = " ".join(
            (
                await session.execute(
                    text(
                        "SELECT c.text FROM chunks c JOIN documents d ON d.id = c.document_id "
                        "WHERE d.title = 'quarterly plan'"
                    )
                )
            )
            .scalars()
            .all()
        )

        for secret in SECRET_VALUES:
            assert secret not in chunk_text, f"{secret!r} survived masking into a chunk"
        assert "[EMAIL_" in chunk_text, "nothing was masked, so this proves nothing"

    async def test_the_original_body_is_retained_behind_the_acl(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """Masking is not redaction. The original stays, behind the ACL check.

        It is what citation offsets address, and losing it would make every highlight
        point into a string that no longer exists (ADR 0005).
        """
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        row = (
            await session.execute(
                text(
                    "SELECT body_original, body_masked FROM documents "
                    "WHERE title = 'quarterly plan'"
                )
            )
        ).one()

        assert "ada@example.com" in row.body_original
        assert "ada@example.com" not in row.body_masked

    async def test_chunk_offsets_address_the_original_body(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """The trap CLAUDE.md records, asserted against stored rows.

        Offsets index the ORIGINAL document while the stored text is masked, and the two
        have different lengths. Every offset must therefore be within the original's
        bounds, which is exactly what a masked-coordinate bug would break.
        """
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        rows = (
            await session.execute(
                text(
                    "SELECT c.char_start, c.char_end, length(d.body_original) AS original_length "
                    "FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE d.title = 'quarterly plan' ORDER BY c.ordinal"
                )
            )
        ).all()

        assert rows
        for row in rows:
            assert 0 <= row.char_start <= row.char_end <= row.original_length


class TestTenantIsolation:
    async def test_a_source_run_cannot_reach_another_tenant(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """A source id from another tenant is simply not found."""
        alpha = await make_tenant(session, "grace", corpus)
        beta = await make_tenant(session, "eve", corpus)

        await scope(session, beta["org_id"])
        with pytest.raises(Exception):  # noqa: B017 - UnsupportedSource
            await run_source_job(
                session,
                org_id=beta["org_id"],
                source_id=alpha["source_id"],
                correlation_id="c",
            )

    async def test_ingested_documents_stay_in_their_tenant(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        alpha = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, alpha)

        # A second tenant, scoped to itself by `make_tenant`.
        await make_tenant(session, "eve", corpus)

        assert (await counts(session))["documents"] == 0, "another tenant's documents are visible"
        await scope(session, alpha["org_id"])
        assert (await counts(session))["documents"] == 2


class TestFailureHandling:
    async def test_a_malformed_document_fails_permanently(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """It will never parse, so retrying reaches the same conclusion five times."""
        root = write_corpus(tmp_path / "maildir", {"broken/1.": b"\x00\x01 not mail at all"})
        tenant = await make_tenant(session, "grace", root)
        await walk_source(session, tenant)
        job = await claim_job(session, kind=JobKind.INGEST_DOCUMENT)
        assert job is not None

        from jutsu_worker.ingest import record_failure

        try:
            await run_document_job(session, job=job)
        except Exception as error:
            state = await record_failure(session, job=job, error=error)
        else:  # pragma: no cover - the fixture is deliberately unparsable
            pytest.fail("a corrupt file was ingested without error")

        assert state is JobState.FAILED
        row = (
            await session.execute(
                text("SELECT failure_kind FROM jobs WHERE id = :i"), {"i": str(job.id)}
            )
        ).scalar_one()
        assert row == FailureKind.MALFORMED_DOCUMENT.value
        assert await claim_job(session, kind=JobKind.INGEST_DOCUMENT) is None

    async def test_a_missing_source_file_is_a_source_failure(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """Enqueued, then deleted before the worker got to it. Retry may well help."""
        tenant = await make_tenant(session, "grace", corpus)
        await walk_source(session, tenant)
        (corpus / "grace" / "1.").unlink()
        (corpus / "eve" / "1.").unlink()

        from jutsu_worker.ingest import record_failure

        job = await claim_job(session, kind=JobKind.INGEST_DOCUMENT)
        assert job is not None
        try:
            await run_document_job(session, job=job)
        except Exception as error:
            state = await record_failure(session, job=job, error=error)
        else:  # pragma: no cover
            pytest.fail("a deleted file was ingested")

        assert state is JobState.RETRY_SCHEDULED
        kind = (
            await session.execute(
                text("SELECT failure_kind FROM jobs WHERE id = :i"), {"i": str(job.id)}
            )
        ).scalar_one()
        assert kind == FailureKind.SOURCE_UNAVAILABLE.value

    async def test_a_failed_document_leaves_no_partial_rows(
        self, session: AsyncSession, tmp_path: Path
    ) -> None:
        """Nothing half-written: no document without its grants, no chunks without a body."""
        root = write_corpus(tmp_path / "maildir", {"broken/1.": b"\x00 not mail"})
        tenant = await make_tenant(session, "grace", root)
        await walk_source(session, tenant)
        job = await claim_job(session, kind=JobKind.INGEST_DOCUMENT)
        assert job is not None

        with pytest.raises(Exception):  # noqa: B017
            await run_document_job(session, job=job)

        after = await counts(session)
        assert after["documents"] == 0
        assert after["chunks"] == 0
        assert after["document_acl"] == 0


class TestEmbeddingBoundary:
    async def test_an_embedding_failure_does_not_refetch_the_document(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """**The regression test for §9's central requirement.**

        A failure at embedding must never force a re-fetch. The proof is structural rather
        than behavioural: the document job is already `completed` and its rows are durable
        before the embedding job exists, so a failing embedding job cannot put the
        document job back into a claimable state. The connector is then deleted outright —
        if anything tried to fetch again it would raise, and nothing does.
        """
        tenant = await make_tenant(session, "grace", corpus)
        await walk_source(session, tenant)
        await drain_documents(tenant)
        documents_before = (await counts(session))["documents"]

        # The corpus disappears. A re-fetch is now impossible, not merely unwanted.
        # ASYNC240: filesystem work in an async test, deliberately — the point is that
        # the files are gone before the embedding stage runs, and a thread hop would
        # add nothing but indirection to a two-file tmp_path.
        for path in sorted(corpus.rglob("*.")):  # noqa: ASYNC240
            path.unlink()

        transport = FakeTransport(failures=[TransientEmbeddingError("429", status=429)] * 10)
        job = await claim_job(session, kind=JobKind.EMBED_DOCUMENT)
        assert job is not None

        from jutsu_worker.ingest import record_failure

        try:
            await run_embedding_job(session, job=job, embedder=embedder(transport))
        except Exception as error:
            state = await record_failure(session, job=job, error=error)
        else:  # pragma: no cover
            pytest.fail("the scripted transient failure did not surface")

        assert state is JobState.RETRY_SCHEDULED
        assert (await counts(session))["documents"] == documents_before
        # No ingest.document job returned to a claimable state.
        assert await claim_job(session, kind=JobKind.INGEST_DOCUMENT) is None

    async def test_a_transient_provider_failure_is_retryable(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        tenant = await make_tenant(session, "grace", corpus)
        await walk_source(session, tenant)
        await drain_documents(tenant)

        from jutsu_worker.ingest import record_failure

        transport = FakeTransport(failures=[TransientEmbeddingError("503", status=503)] * 10)
        job = await claim_job(session, kind=JobKind.EMBED_DOCUMENT)
        assert job is not None
        try:
            await run_embedding_job(session, job=job, embedder=embedder(transport))
        except Exception as error:
            await record_failure(session, job=job, error=error)

        kind = (
            await session.execute(
                text("SELECT failure_kind FROM jobs WHERE id = :i"), {"i": str(job.id)}
            )
        ).scalar_one()
        assert kind == FailureKind.EMBEDDING_TRANSIENT.value

    async def test_a_permanent_provider_failure_is_not_retried(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """A 400 is rejected identically every time; retrying spends quota to learn that."""
        tenant = await make_tenant(session, "grace", corpus)
        await walk_source(session, tenant)
        await drain_documents(tenant)

        transport = FakeTransport(failures=[PermanentEmbeddingError("400", status=400)])
        assert await process_embedding(tenant["org_id"], embedder(transport)) is None

        await refresh(session, tenant)
        failed = (
            await session.execute(
                text(
                    "SELECT id, state, failure_kind, next_attempt_at FROM jobs "
                    "WHERE kind = :k AND state = :s"
                ),
                {"k": JobKind.EMBED_DOCUMENT.value, "s": JobState.FAILED.value},
            )
        ).one()

        assert failed.failure_kind == FailureKind.EMBEDDING_PERMANENT.value
        assert failed.next_attempt_at is None, "a permanent failure was scheduled for retry"
        # The corpus has two documents, so a second embedding job is legitimately still
        # queued. What must never happen is *this* job coming back — asserted by id
        # rather than by "nothing is claimable", which would have passed for the wrong
        # reason if the other job had been drained first.
        again = await claim_job(
            session, kind=JobKind.EMBED_DOCUMENT, job_id=uuid.UUID(str(failed.id))
        )
        assert again is None, "a permanent provider failure was retried"

    async def test_a_partial_embedding_resumes_without_redoing_work(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """Resumability is the selection, not a checkpoint.

        Half the chunks are embedded by hand, then the job runs. It must embed only what
        is still NULL — which is what makes a retried embedding job cheap rather than a
        full re-spend.
        """
        tenant = await make_tenant(session, "grace", corpus)
        await walk_source(session, tenant)
        await drain_documents(tenant)

        vector = "[" + ",".join(["0.5"] * DIM) + "]"
        await session.execute(
            text(
                "UPDATE chunks SET embedding = CAST(:v AS vector) WHERE id IN "
                "(SELECT id FROM chunks ORDER BY id LIMIT 1)"
            ),
            {"v": vector},
        )
        already = (
            await session.execute(text("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL"))
        ).scalar_one()
        remaining = (
            await session.execute(text("SELECT count(*) FROM chunks WHERE embedding IS NULL"))
        ).scalar_one()
        await session.commit()
        await scope(session, tenant["org_id"])

        transport = FakeTransport()
        written = await drain_embeddings(tenant, transport)

        assert already == 1
        assert written == remaining, "the job re-embedded chunks that already had vectors"


class TestCursorAtomicity:
    async def test_a_failed_run_advances_nothing(self, session: AsyncSession, corpus: Path) -> None:
        """The cursor and the enqueued jobs are one fact, so they fail together.

        The run is rolled back mid-transaction, which is what a crash between the walk and
        the commit looks like. Neither the jobs nor the cursor may survive it — a cursor
        that advanced without its jobs skips those documents for ever.
        """
        tenant = await make_tenant(session, "grace", corpus)
        await session.commit()

        await scope(session, tenant["org_id"])
        # `run_source_job` directly, never the committing helper: this test is about what
        # survives a transaction that never commits.
        await run_source_job(
            session,
            org_id=tenant["org_id"],
            source_id=tenant["source_id"],
            correlation_id="crash",
        )
        await session.rollback()

        await scope(session, tenant["org_id"])
        cursor = (
            await session.execute(
                text("SELECT last_sync_cursor FROM sources WHERE id = :i"),
                {"i": str(tenant["source_id"])},
            )
        ).scalar_one()
        queued = (await counts(session))["jobs"]

        assert cursor is None, "the cursor advanced despite the run being rolled back"
        assert queued == 0, "jobs survived a rolled-back run"

    async def test_a_successful_run_advances_the_cursor(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        tenant = await make_tenant(session, "grace", corpus)

        await walk_source(session, tenant)

        cursor = (
            await session.execute(
                text("SELECT last_sync_cursor FROM sources WHERE id = :i"),
                {"i": str(tenant["source_id"])},
            )
        ).scalar_one()
        assert cursor is not None


class TestCrashRecovery:
    async def test_a_source_run_reclaims_this_tenants_crashed_jobs(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """Where recovery happens, in the absence of a global sweeper."""
        tenant = await make_tenant(session, "grace", corpus)
        await walk_source(session, tenant)
        crashed = await claim_job(session, kind=JobKind.INGEST_DOCUMENT)
        assert crashed is not None
        await session.execute(
            text("UPDATE jobs SET locked_until = now() - INTERVAL '1 second' WHERE id = :i"),
            {"i": str(crashed.id)},
        )

        run = await walk_source(session, tenant)

        assert run.reclaimed == 1
        recovered = await claim_job(session, kind=JobKind.INGEST_DOCUMENT, job_id=crashed.id)
        assert recovered is not None
        assert await run_document_job(session, job=recovered) is IngestOutcome.CREATED

    async def test_redis_losing_a_message_loses_no_work(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """Postgres is the source of truth; the queue is a doorbell.

        Nothing here touches Redis, and that is the test: the job rows exist, so a worker
        polling the durable queue finds and completes every one of them. A dispatch
        message that was never delivered changes nothing.
        """
        tenant = await make_tenant(session, "grace", corpus)
        await walk_source(session, tenant)

        outcomes = await drain_documents(tenant)

        assert len(outcomes) == 2
        assert (await counts(session))["documents"] == 2


class TestObservability:
    async def test_audit_rows_are_written_for_the_run(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        actions = (
            (await session.execute(text("SELECT action FROM audit_log ORDER BY id")))
            .scalars()
            .all()
        )

        assert "ingest.source.completed" in actions
        assert "ingest.document.created" in actions
        assert "embed.document.completed" in actions

    async def test_audit_rows_carry_a_stable_correlation_id(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """A retried job must stay findable, so the id is the job id, never a fresh one."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        rows = (
            (
                await session.execute(
                    text(
                        "SELECT correlation_id FROM audit_log WHERE action LIKE 'ingest.document.%'"
                    )
                )
            )
            .scalars()
            .all()
        )

        assert all(row for row in rows), "a pipeline audit row has no correlation id"

    async def test_no_document_content_reaches_the_audit_trail(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """§4.9 — the audit log is exported, so it is governed exactly as logs are."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        dumped = str(
            (await session.execute(text("SELECT to_jsonb(audit_log) FROM audit_log"))).all()
        )

        for secret in SECRET_VALUES:
            assert secret not in dumped
        assert "quarterly plan" not in dumped, "a document title reached the audit trail"
        assert "local:grace@example.com" not in dumped, "a principal reached the audit trail"

    async def test_no_sensitive_content_reaches_the_logs(
        self, session: AsyncSession, corpus: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Counts and opaque ids only — never a body, a chunk, a principal or a vector."""
        tenant = await make_tenant(session, "grace", corpus)
        with caplog.at_level(logging.INFO, logger="jutsu"):
            await ingest_everything(session, tenant)

        emitted = "\n".join(record.getMessage() for record in caplog.records)

        assert emitted, "nothing was logged, so this proves nothing"
        for secret in SECRET_VALUES:
            assert secret not in emitted
        assert "quarterly plan" not in emitted
        assert "local:" not in emitted
        assert "[EMAIL_" not in emitted, "masked chunk text reached the logs"


class TestRetrievalIntegration:
    async def test_ingested_content_is_retrievable_by_an_authorized_user(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """The whole point, end to end.

        Grace holds `local:grace@example.com`; the mail granted exactly that principal.
        If any stage lost the grant, the namespace, the org scope or the vector, this
        returns nothing.
        """
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        page = await search_chunks(
            session, user_id=tenant["user_id"], query_vector=[1.0] + [0.0] * (DIM - 1), k=50
        )

        titles = {item.document_title for item in page.items}
        assert "quarterly plan" in titles

    async def test_an_unauthorized_document_stays_invisible(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """Eve's message is in the same tenant, ingested by the same run, granted elsewhere."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        page = await search_chunks(
            session, user_id=tenant["user_id"], query_vector=[1.0] + [0.0] * (DIM - 1), k=50
        )

        titles = {item.document_title for item in page.items}
        assert "private note" not in titles, "an unauthorized document was retrieved"

    async def test_retrieved_evidence_is_masked(self, session: AsyncSession, corpus: Path) -> None:
        """What retrieval hands to a model must be the masked copy."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)

        page = await search_chunks(
            session, user_id=tenant["user_id"], query_vector=[1.0] + [0.0] * (DIM - 1), k=50
        )

        body = " ".join(item.text for item in page.items)
        assert body, "nothing was retrieved, so this proves nothing"
        for secret in SECRET_VALUES:
            assert secret not in body

    async def test_a_revoked_identity_loses_the_ingested_content(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """S6.6's revocation switch, over content this pipeline actually ingested."""
        tenant = await make_tenant(session, "grace", corpus)
        await ingest_everything(session, tenant)
        before = await search_chunks(
            session, user_id=tenant["user_id"], query_vector=[1.0] + [0.0] * (DIM - 1), k=50
        )
        assert before.items

        await session.execute(
            text("UPDATE source_identities SET is_active = false WHERE user_id = :u"),
            {"u": tenant["user_id"]},
        )

        after = await search_chunks(
            session, user_id=tenant["user_id"], query_vector=[1.0] + [0.0] * (DIM - 1), k=50
        )
        assert after.items == ()


class TestSeedCommand:
    """`make seed`, run twice. The M1 gate's "a second seed adds zero rows"."""

    async def test_seeding_twice_adds_nothing(self, session: AsyncSession, corpus: Path) -> None:
        tenant = await make_tenant(session, "grace", corpus)
        await session.commit()
        await scope(session, tenant["org_id"])

        first = await seed(tenant["org_id"], str(corpus), embed=False)
        await refresh(session, tenant)
        after_first = await counts(session)

        second = await seed(tenant["org_id"], str(corpus), embed=False)
        await refresh(session, tenant)
        after_second = await counts(session)

        assert first == 2, "the first seed ingested nothing, so this proves nothing"
        # Zero, and that is the cursor doing its job rather than the pipeline skipping
        # work. `list_since` compares each file's mtime against the cursor the first run
        # wrote, so an unchanged corpus lists nothing and there is nothing to reopen.
        # The row counts below are what "adds nothing" actually means.
        assert second == 0
        assert after_second["documents"] == after_first["documents"]
        assert after_second["chunks"] == after_first["chunks"]
        assert after_second["document_acl"] == after_first["document_acl"]

    async def test_seeding_reuses_the_same_source_row(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """A fresh source each run would make every document look new.

        `ensure_source` keys on the resolved corpus path, so seeding a directory that
        already has a source row reuses it — cursor, document identities and all. The
        count staying at one across two seeds *and* an existing fixture source is the
        whole assertion.
        """
        tenant = await make_tenant(session, "grace", corpus)
        await session.commit()
        await scope(session, tenant["org_id"])

        await seed(tenant["org_id"], str(corpus), embed=False)
        await seed(tenant["org_id"], str(corpus), embed=False)

        await refresh(session, tenant)
        sources = (
            await session.execute(text("SELECT count(*) FROM sources WHERE system = 'local'"))
        ).scalar_one()
        assert sources == 1, "seeding the same corpus created a second source row"

    async def test_seeding_leaves_chunks_pending_without_embed(
        self, session: AsyncSession, corpus: Path
    ) -> None:
        """Embedding is opt-in because it is the only stage that spends money."""
        tenant = await make_tenant(session, "grace", corpus)
        await session.commit()
        await scope(session, tenant["org_id"])

        await seed(tenant["org_id"], str(corpus), embed=False)

        await refresh(session, tenant)
        pending = (
            await session.execute(text("SELECT count(*) FROM chunks WHERE embedding IS NULL"))
        ).scalar_one()
        assert pending > 0, "seed embedded without being asked to"
