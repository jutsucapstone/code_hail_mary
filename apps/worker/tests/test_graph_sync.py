"""Projecting extraction claims into the graph (ADR 0022).

Against both real stores, because the projection is the join between them: Postgres holds
the claims and decides which run is current, Neo4j holds the result, and the interesting
behaviour is what happens when the two disagree — a re-extraction that finds less, a
document that has been superseded, a graph that is not there at all.

**The last assertion in each class is the one that matters.** This stage is optional by
construction: nothing upstream of it may notice whether it ran, failed, or was never
enqueued. A test here that only proved edges get written would miss the entire point.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from jutsu_db.engine import dispose_engine, org_session
from jutsu_graph.driver import close_driver, get_graph_settings, read_session, write_session
from jutsu_graph.knowledge import CLAIM_LABELS
from jutsu_worker.extraction import CLAIM_TYPES, EXTRACTOR_VERSION
from jutsu_worker.graph_sync import graph_configured, graph_sync_job_key, sync_document_graph
from jutsu_worker.jobs import JobKind, JobState, enqueue_job
from jutsu_worker.runner import process_graph_sync
from sqlalchemy import text

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"
GRAPH_REACHABLE_ENV = "JUTSU_GRAPH_REACHABLE"

pytestmark = pytest.mark.usefixtures("worker_database", "clean_graph")


def _alembic_config(url: str) -> Config:
    root = Path(__file__).resolve().parents[3] / "packages" / "db"
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src" / "jutsu_db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
async def worker_database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Migrated schema and an app-role engine, disposed on both sides.

    Inline rather than in a conftest for the reason `test_extraction.py` records: mypy
    refuses a second module named conftest under `apps/`.
    """
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


@pytest.fixture
async def clean_graph() -> AsyncIterator[uuid.UUID]:
    """One organisation's slice of a live graph, removed afterwards.

    Yields the org id so every test writes into a tenant that exists nowhere else — which
    means the store also contains other tenants' data throughout, and the isolation
    assertions mean something.
    """
    if os.environ.get(GRAPH_REACHABLE_ENV) != "1":
        pytest.skip("nothing listening at NEO4J_URI — start Neo4j with `make up`")
    # Read **before** the test runs, not in teardown. One of these tests unsets
    # `NEO4J_URI` to prove the optionality gate, and a teardown that re-read the
    # environment would then fail to clean up — with the error pointing at the fixture
    # rather than at the test that changed the world underneath it.
    settings = get_graph_settings()
    # And start from no pool at all. The driver is a process-wide singleton bound to
    # whichever event loop created it, and every async test gets its own loop — so a
    # driver another module opened and did not close makes the first write here fail on a
    # socket belonging to a loop that has already closed. Closing on teardown alone is
    # not enough: it makes this suite well behaved and leaves it at the mercy of every
    # other one.
    #
    # Suppressed because closing somebody else's dead pool is exactly the thing that
    # raises. `close_driver` clears the cached reference in a `finally`, so the next
    # `get_driver()` builds a fresh one on this test's loop either way.
    with contextlib.suppress(Exception):
        await close_driver()
    org_id = uuid.uuid4()
    try:
        yield org_id
    finally:
        async with write_session(org_id, settings=settings) as session:
            await session.run("MATCH (n) WHERE n.org_id = $org_id DETACH DELETE n")
        await close_driver()


async def seed_document(
    org_id: uuid.UUID, *, title: str = "handbook"
) -> tuple[uuid.UUID, uuid.UUID]:
    """An organisation, a source, a document and one chunk. Returns `(document, chunk)`."""
    async with org_session(org_id) as session:
        await session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i,'alpha')"), {"i": org_id}
        )
        source_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json) "
                "VALUES (:i,:o,'local','{}'::jsonb)"
            ),
            {"i": source_id, "o": org_id},
        )
        document_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO documents (id, org_id, source_id, external_id, title, "
                "content_hash, acl_hash, body_original, body_masked, created_at) "
                "VALUES (:i,:o,:s,:e,:t,'h','a','original','masked',now())"
            ),
            {"i": document_id, "o": org_id, "s": source_id, "e": f"ext-{title}", "t": title},
        )
        chunk_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO chunks (id, document_id, org_id, ordinal, text, char_start, "
                "char_end, token_count) VALUES (:i,:d,:o,0,'passage',0,7,2)"
            ),
            {"i": chunk_id, "d": document_id, "o": org_id},
        )
    return document_id, chunk_id


async def seed_run(
    org_id: uuid.UUID, chunk_id: uuid.UUID, claims: list[tuple[str, str]]
) -> uuid.UUID:
    """One finished extraction run and its claims. Returns the run id."""
    run_id = uuid.uuid4()
    async with org_session(org_id) as session:
        await session.execute(
            text(
                "INSERT INTO extraction_runs (id, org_id, extractor_version, prompt_hash, "
                "model, finished_at) VALUES (:i,:o,:v,'hash','test-model',now())"
            ),
            {"i": run_id, "o": org_id, "v": EXTRACTOR_VERSION},
        )
        for claim_type, name in claims:
            payload = {
                "summary": name,
                "name": name,
                "quote": "passage",
                "char_start": 0,
                "char_end": 7,
                "extractor_version": EXTRACTOR_VERSION,
            }
            await session.execute(
                text(
                    "INSERT INTO extraction_claims (id, run_id, chunk_id, org_id, claim_type, "
                    "payload_json, confidence) "
                    "VALUES (gen_random_uuid(), :r, :c, :o, :t, CAST(:p AS jsonb), 0.8)"
                ),
                {
                    "r": run_id,
                    "c": chunk_id,
                    "o": org_id,
                    "t": claim_type,
                    "p": json.dumps(payload),
                },
            )
    return run_id


async def graph_state(org_id: uuid.UUID) -> list[dict[str, Any]]:
    async with read_session(org_id, settings=get_graph_settings()) as session:
        rows = await session.run(
            "MATCH ()-[r]-(n) WHERE r.org_id = $org_id AND n.org_id = $org_id "
            "AND NOT n:Document "
            "RETURN n.name AS name, labels(n)[0] AS label, r.chunk_id AS chunk_id, "
            "r.valid_to AS valid_to"
        )
    return [dict(row) for row in rows]


class TestProjection:
    async def test_a_documents_claims_become_evidenced_edges(self, clean_graph: uuid.UUID) -> None:
        document_id, chunk_id = await seed_document(clean_graph)
        await seed_run(clean_graph, chunk_id, [("person", "Ada Lovelace"), ("project", "Alpha")])

        async with org_session(clean_graph) as session:
            report = await sync_document_graph(session, org_id=clean_graph, document_id=document_id)

        assert report is not None
        assert report.edges_written == 2
        state = await graph_state(clean_graph)
        assert {row["name"] for row in state} == {"Ada Lovelace", "Alpha"}
        # Provenance: every edge names the chunk that evidenced it.
        assert {row["chunk_id"] for row in state} == {str(chunk_id)}

    async def test_running_it_twice_writes_the_same_graph(self, clean_graph: uuid.UUID) -> None:
        document_id, chunk_id = await seed_document(clean_graph)
        await seed_run(clean_graph, chunk_id, [("person", "Ada Lovelace")])

        async with org_session(clean_graph) as session:
            await sync_document_graph(session, org_id=clean_graph, document_id=document_id)
            first = await graph_state(clean_graph)
            await sync_document_graph(session, org_id=clean_graph, document_id=document_id)

        assert await graph_state(clean_graph) == first

    async def test_a_later_run_supersedes_what_it_no_longer_asserts(
        self, clean_graph: uuid.UUID
    ) -> None:
        # The read model selects the **latest finished run** per document, so a second
        # extraction that no longer mentions Ada must close her edge rather than leave
        # the graph asserting something the evidence no longer supports.
        document_id, chunk_id = await seed_document(clean_graph)
        await seed_run(clean_graph, chunk_id, [("person", "Ada Lovelace"), ("project", "Alpha")])

        async with org_session(clean_graph) as session:
            await sync_document_graph(session, org_id=clean_graph, document_id=document_id)

        await seed_run(clean_graph, chunk_id, [("project", "Alpha")])
        async with org_session(clean_graph) as session:
            await sync_document_graph(session, org_id=clean_graph, document_id=document_id)

        state = {row["name"]: row["valid_to"] for row in await graph_state(clean_graph)}
        assert state["Alpha"] is None
        # Closed, never deleted (§7).
        assert state["Ada Lovelace"] is not None

    async def test_a_superseded_claim_is_not_projected(self, clean_graph: uuid.UUID) -> None:
        document_id, chunk_id = await seed_document(clean_graph)
        await seed_run(clean_graph, chunk_id, [("person", "Ada Lovelace")])
        async with org_session(clean_graph) as session:
            await session.execute(
                text("UPDATE extraction_claims SET superseded_by = id WHERE chunk_id = :c"),
                {"c": chunk_id},
            )

        async with org_session(clean_graph) as session:
            report = await sync_document_graph(session, org_id=clean_graph, document_id=document_id)

        assert report is not None
        assert report.edges_written == 0

    async def test_a_document_that_is_gone_is_not_a_failure(self, clean_graph: uuid.UUID) -> None:
        # A superseded document's row can be replaced between the extraction and this
        # job. Nothing to project, nothing to retry.
        await seed_document(clean_graph)

        async with org_session(clean_graph) as session:
            report = await sync_document_graph(
                session, org_id=clean_graph, document_id=uuid.uuid4()
            )

        assert report is None

    async def test_a_claim_with_no_name_writes_no_node(self, clean_graph: uuid.UUID) -> None:
        document_id, chunk_id = await seed_document(clean_graph)
        await seed_run(clean_graph, chunk_id, [("person", "")])

        async with org_session(clean_graph) as session:
            report = await sync_document_graph(session, org_id=clean_graph, document_id=document_id)

        assert report is not None
        assert report.edges_written == 0


class TestTheJob:
    async def test_a_queued_job_is_claimed_and_completed(self, clean_graph: uuid.UUID) -> None:
        document_id, chunk_id = await seed_document(clean_graph)
        await seed_run(clean_graph, chunk_id, [("project", "Alpha")])
        async with org_session(clean_graph) as session:
            await enqueue_job(
                session,
                org_id=clean_graph,
                kind=JobKind.GRAPH_DOCUMENT,
                idempotency_key=graph_sync_job_key(clean_graph, document_id),
                payload={"document_id": str(document_id)},
            )

        written = await process_graph_sync(clean_graph)

        assert written == 1
        async with org_session(clean_graph) as session:
            state = (
                await session.execute(
                    text("SELECT state FROM jobs WHERE kind = :k"),
                    {"k": JobKind.GRAPH_DOCUMENT.value},
                )
            ).scalar_one()
        assert state == JobState.COMPLETED.value

    async def test_an_empty_queue_returns_none(self, clean_graph: uuid.UUID) -> None:
        # `None` means "nothing claimable", which the drain loop treats differently from
        # a failure — the distinction `_JobFailed` exists for.
        assert await process_graph_sync(clean_graph) is None


class TestOptionality:
    def test_the_gate_reads_the_connection_details(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEO4J_URI", raising=False)
        assert graph_configured() is False

    def test_the_gate_is_satisfied_by_a_configured_deployment(self) -> None:
        # The fixture skips unless a graph is reachable, so the environment really does
        # carry connection details here.
        assert graph_configured() is True

    async def test_extraction_enqueues_a_graph_job_when_configured(
        self, clean_graph: uuid.UUID
    ) -> None:
        # The link in the chain, asserted at the point it is made: extraction completing
        # is what queues the projection.
        document_id, chunk_id = await seed_document(clean_graph)
        await seed_run(clean_graph, chunk_id, [("project", "Alpha")])

        from jutsu_worker.runner import process_extraction

        async with org_session(clean_graph) as session:
            await enqueue_job(
                session,
                org_id=clean_graph,
                kind=JobKind.EXTRACT_DOCUMENT,
                idempotency_key=f"extract.document:{clean_graph}:{document_id}",
                payload={"document_id": str(document_id)},
            )

        class Silent:
            async def complete(self, *, system: str, prompt: str) -> str:
                return '{"claims": []}'

        await process_extraction(clean_graph, transport=Silent())

        async with org_session(clean_graph) as session:
            queued = (
                await session.execute(
                    text("SELECT count(*) FROM jobs WHERE kind = :k"),
                    {"k": JobKind.GRAPH_DOCUMENT.value},
                )
            ).scalar_one()
        assert int(queued) == 1


class TestTheMappingStaysHonest:
    def test_every_extraction_claim_type_reaches_the_graph(self) -> None:
        """The two lists must agree, and nothing else makes them.

        The failure this catches is quiet by construction: somebody adds a sixth claim
        type to extraction, the extractor starts emitting it, and `plan_edges` raises on
        a background job nobody is watching — or worse, silently skips it. Pinning the
        sets here means the omission fails in CI, in the package that owns the prompt.
        """
        assert set(CLAIM_TYPES) == set(CLAIM_LABELS)
