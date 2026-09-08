"""An uploaded file, all the way to a searchable chunk, against real Postgres.

This is the claim the whole feature rests on: a Knowledge Basket file goes through the
*same* pipeline a connector's document does — versioned by `content_hash`, masked,
chunked, ACL-granted — rather than a second path that would drift from it. Asserting it
anywhere but against the real `run_document_job` and the real tables would prove only
that the code compiles.

The bucket is a dict. What needs the database is the document, the chunks and the grant;
what the store does with bytes is proven in `packages/core/tests/test_storage.py`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from jutsu_core.storage import object_key
from jutsu_db.engine import dispose_engine
from jutsu_worker.jobs import JobKind, enqueue_job
from jutsu_worker.pipeline import IngestOutcome
from jutsu_worker.runner import process_document
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"


# The migration harness, spelled out rather than imported from `test_ingest_pipeline`.
# A cross-module test import makes mypy see the same file under two module names, and
# the alternative — a shared `_harness` module — is a refactor of a large existing suite
# for four short functions. `test_ingest_pipeline`'s fixture is the one that documents
# WHY each step is here; this is the same shape.
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
    """Alembic's env.py ends in `asyncio.run`, which cannot nest inside a running loop."""
    fn = command.upgrade if direction == "upgrade" else command.downgrade
    await asyncio.to_thread(fn, cfg, revision)


async def clear_tenant_data(url: str) -> None:
    """Remove every tenant row, if the tables are there at all.

    Migration 0010's downgrade refuses to run while any document has been superseded —
    correctly, because restoring the old non-partial constraint over real version history
    would destroy it. So a suite that creates versions has to clear them itself.
    """
    owner = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with owner.connect() as connection:
            await connection.execute(
                text("TRUNCATE documents, sources, jobs, audit_log, orgs CASCADE")
            )
    except Exception:  # noqa: S110 — see below
        # The tables may not exist yet on the first run, and "nothing to clear" is
        # success. Deliberately not logged: this runs in a fixture before every test in
        # the file, and a line per run would bury the output that matters.
        pass
    finally:
        await owner.dispose()


class FakeStore:
    """The bucket, as a dict of key to bytes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def download(self, key: str, *, max_bytes: int) -> bytes:
        return self.objects.get(key, b"")[:max_bytes]


@pytest.fixture
async def session(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncSession]:
    """One transaction as the restricted `jutsu_app` role, over a freshly migrated schema.

    The same shape as `test_ingest_pipeline.py`'s, and for its reasons: the app role is
    what makes the isolation assertions non-vacuous, and `dispose_engine` on both sides is
    what stops a cached pool outliving the schema it was bound to.
    """
    skip_without_database()
    app_url = os.environ[TEST_DB_ENV]
    migration_url = os.environ.get(MIGRATION_DB_ENV, app_url)

    monkeypatch.setenv("DATABASE_URL", migration_url)
    cfg = alembic_config(migration_url)
    await clear_tenant_data(migration_url)
    await run_alembic(cfg, "downgrade", "base")
    await run_alembic(cfg, "upgrade", "head")

    monkeypatch.setenv("DATABASE_URL", app_url)
    await dispose_engine()

    engine = create_async_engine(app_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    # No `begin()`: the pipeline's real shape is several transactions in sequence, so a
    # test that could not commit could only exercise a shape production never runs.
    async with factory() as opened:
        yield opened
        await opened.rollback()

    await engine.dispose()
    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", migration_url)
    await clear_tenant_data(migration_url)


async def scope(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
    )


async def a_basket(
    session: AsyncSession,
    store: FakeStore,
    *,
    body: bytes,
    filename: str = "handover.txt",
    mime: str = "text/plain",
) -> dict[str, Any]:
    """An organisation, a person, a basket source, and one uploaded file."""
    org_id, user_id, source_id, file_id = (uuid.uuid4() for _ in range(4))
    await scope(session, org_id)
    await session.execute(
        text("INSERT INTO orgs (id, name) VALUES (:i, :n)"), {"i": org_id, "n": "Basket Co"}
    )
    await session.execute(
        text("INSERT INTO users (id, org_id, email, status) VALUES (:i,:o,:e,'active')"),
        {"i": user_id, "o": org_id, "e": "ada@example.com"},
    )
    await session.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:i, :o, 'basket', '{}'::jsonb)"
        ),
        {"i": source_id, "o": org_id},
    )

    key = object_key(org_id, file_id)
    store.objects[key] = body
    await session.execute(
        text(
            "INSERT INTO basket_files (id, org_id, owner_user_id, object_key, "
            "original_filename, normalised_filename, declared_mime, detected_mime, "
            "size_bytes, state) VALUES (:i,:o,:u,:k,:f,:n,:m,:m,:s,'uploaded')"
        ),
        {
            "i": file_id,
            "o": org_id,
            "u": user_id,
            "k": key,
            "f": filename,
            "n": filename.lower(),
            "m": mime,
            "s": len(body),
        },
    )
    await session.commit()
    return {
        "org_id": org_id,
        "user_id": user_id,
        "source_id": source_id,
        "file_id": file_id,
        "principal": f"basket:{user_id}",
    }


async def queue_and_run(session: AsyncSession, basket: dict[str, Any], store: FakeStore) -> object:
    """Enqueue the job the API would, then run the real worker handler.

    Returns whatever `process_document` did — an `IngestOutcome`, its `JOB_FAILED`
    sentinel, or None. Typed loosely on purpose: a test that narrowed it here would hide
    a failure behind a type error instead of asserting on it.
    """
    # **Re-scope after every commit.** `set_config(..., true)` is TRANSACTION-scoped, so
    # the commit at the end of `a_basket` cleared it — and `jobs` is FORCE RLS, so an
    # insert with no tenant set is refused rather than mis-filed. Exactly the trap
    # CLAUDE.md records about `NULLIF(current_setting(...), '')`.
    await scope(session, basket["org_id"])
    await enqueue_job(
        session,
        org_id=basket["org_id"],
        kind=JobKind.INGEST_DOCUMENT,
        idempotency_key=(
            f"ingest.document:{basket['org_id']}:{basket['source_id']}:{basket['file_id']}"
        ),
        payload={
            "source_id": str(basket["source_id"]),
            "external_id": str(basket["file_id"]),
        },
    )
    await session.commit()

    # The worker builds its store from the environment inside `resolve_connector`, so the
    # only seam a test has is that constructor. Patched with `setattr` rather than an
    # annotated assignment because mypy checks the latter against the real signature, and
    # a `FakeStore` is deliberately not an `ObjectStore` — it implements the two methods
    # the reader calls and nothing else.
    import jutsu_core.storage as storage_module

    original = storage_module.ObjectStore.from_env
    setattr(storage_module.ObjectStore, "from_env", classmethod(lambda cls: store))  # noqa: B010
    try:
        return await process_document(basket["org_id"])
    finally:
        setattr(storage_module.ObjectStore, "from_env", original)  # noqa: B010


class TestAnUploadedFileBecomesSearchableKnowledge:
    async def test_it_creates_a_document_with_chunks_and_one_grant(
        self, session: AsyncSession
    ) -> None:
        store = FakeStore()
        basket = await a_basket(
            session, store, body=b"The migration to Postgres 16 was decided in March 2026."
        )

        outcome = await queue_and_run(session, basket, store)

        assert outcome is IngestOutcome.CREATED
        await scope(session, basket["org_id"])

        document = (
            await session.execute(
                text(
                    "SELECT id, external_id, title, mime, body_original "
                    "FROM documents WHERE source_id = :s"
                ),
                {"s": basket["source_id"]},
            )
        ).one()
        assert str(document.external_id) == str(basket["file_id"])
        assert document.title == "handover.txt"
        assert "migration to Postgres 16" in document.body_original

        chunks = (
            await session.execute(
                text("SELECT count(*) FROM chunks WHERE document_id = :d"),
                {"d": document.id},
            )
        ).scalar_one()
        assert chunks >= 1, "no chunks means nothing to search"

        grants = (
            await session.execute(
                text(
                    "SELECT principal_type, principal_id FROM document_acl WHERE document_id = :d"
                ),
                {"d": document.id},
            )
        ).all()
        # Exactly one, naming the uploader. A wider grant would be a guess wearing an ACL.
        assert [(g.principal_type, g.principal_id) for g in grants] == [
            ("user", basket["principal"])
        ]

    async def test_it_queues_the_embedding_as_its_own_job(self, session: AsyncSession) -> None:
        # The two stages are separate rows on purpose: re-running an embedding must never
        # re-read the object.
        store = FakeStore()
        basket = await a_basket(session, store, body=b"Some text worth embedding.")

        await queue_and_run(session, basket, store)
        await scope(session, basket["org_id"])

        queued = (
            await session.execute(text("SELECT count(*) FROM jobs WHERE kind = 'embed.document'"))
        ).scalar_one()
        assert queued == 1

    async def test_a_real_docx_arrives_as_searchable_text(self, session: AsyncSession) -> None:
        import io

        docx = pytest.importorskip("docx")
        document = docx.Document()
        document.add_paragraph("The runbook lives in Confluence under Platform.")
        buffer = io.BytesIO()
        document.save(buffer)

        store = FakeStore()
        basket = await a_basket(
            session,
            store,
            body=buffer.getvalue(),
            filename="runbook.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        outcome = await queue_and_run(session, basket, store)

        assert outcome is IngestOutcome.CREATED
        await scope(session, basket["org_id"])
        body = (
            await session.execute(
                text("SELECT body_original FROM documents WHERE source_id = :s"),
                {"s": basket["source_id"]},
            )
        ).scalar_one()
        assert "runbook lives in Confluence" in body

    async def test_re_running_the_same_file_writes_nothing_new(self, session: AsyncSession) -> None:
        """Idempotency, decided by `content_hash` inside `persist_document`.

        A repeated ingestion of unchanged bytes must produce no second document, no
        second chunk set and no second embedding job.
        """
        store = FakeStore()
        basket = await a_basket(session, store, body=b"Unchanged content.")
        await queue_and_run(session, basket, store)

        await scope(session, basket["org_id"])
        before = {
            table: (
                await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            ).scalar_one()
            for table in ("documents", "chunks", "document_acl")
        }

        # Reopen the completed job the way the API's retry does, then run it again.
        await session.execute(
            text(
                "UPDATE jobs SET state = 'pending', attempts = 0, locked_until = NULL "
                "WHERE kind = 'ingest.document'"
            )
        )
        await session.commit()
        second = await queue_and_run(session, basket, store)

        assert second is IngestOutcome.UNCHANGED
        await scope(session, basket["org_id"])
        after = {
            table: (
                await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            ).scalar_one()
            for table in ("documents", "chunks", "document_acl")
        }
        assert after == before

    async def test_a_deleted_file_completes_rather_than_retrying_for_ever(
        self, session: AsyncSession
    ) -> None:
        """`DocumentGone` is the difference between one wasted run and five.

        A file removed between the job being queued and run must terminate the job, not
        occupy the queue until it dead-letters.
        """
        store = FakeStore()
        basket = await a_basket(session, store, body=b"About to be deleted.")
        # Re-scope: `a_basket` committed, which cleared the transaction-scoped GUC, and
        # an unscoped UPDATE would be filtered by RLS to zero rows — leaving the file
        # very much not deleted and the assertion below quietly meaningless.
        await scope(session, basket["org_id"])
        await session.execute(
            text(
                "UPDATE basket_files SET deleted_at = now(), deleted_by = owner_user_id "
                "WHERE id = :i"
            ),
            {"i": basket["file_id"]},
        )
        await session.commit()

        outcome = await queue_and_run(session, basket, store)

        assert outcome is IngestOutcome.ABSENT
        await scope(session, basket["org_id"])
        state = (
            await session.execute(text("SELECT state FROM jobs WHERE kind = 'ingest.document'"))
        ).scalar_one()
        assert state == "completed", "a gone document must end the job, not retry it"


class TestTenantIsolation:
    async def test_a_basket_file_is_invisible_to_another_organisation(
        self, session: AsyncSession
    ) -> None:
        """Row-level security, checked by scoping to a different tenant and looking."""
        store = FakeStore()
        basket = await a_basket(session, store, body=b"Confidential to one tenant.")
        await queue_and_run(session, basket, store)

        other_org = uuid.uuid4()
        # Scoped BEFORE the insert: `orgs` carries a WITH CHECK policy, so a row whose id
        # is not the current tenant is refused. Which is itself the isolation working.
        await scope(session, other_org)
        await session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i, :n)"),
            {"i": other_org, "n": "Somebody Else"},
        )

        files = (await session.execute(text("SELECT count(*) FROM basket_files"))).scalar_one()
        documents = (await session.execute(text("SELECT count(*) FROM documents"))).scalar_one()
        chunks = (await session.execute(text("SELECT count(*) FROM chunks"))).scalar_one()

        assert files == 0, "another tenant could see basket rows"
        assert documents == 0, "another tenant could see the documents they became"
        assert chunks == 0, "another tenant could see the chunks"
