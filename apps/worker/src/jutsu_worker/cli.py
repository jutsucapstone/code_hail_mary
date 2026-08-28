"""`make seed` — run the ingestion pipeline over a local corpus, idempotently.

    uv run --package jutsu-worker python -m jutsu_worker.cli seed --org <uuid> --root <dir>

**Running it twice adds nothing.** The second run lists the same identifiers, finds the
same job keys, computes the same content hashes and writes no rows — which is §4.14's
requirement expressed as a command rather than as a claim. It is the M1 gate's "a second
`make seed` adds zero rows", and it is checked by the test suite rather than by reading
this paragraph.

**It refuses to invent a corpus.** `--root` must exist and must contain mail; there is no
bundled sample and no generated stand-in. A seed command that quietly produced synthetic
documents would put fabricated data behind every downstream measurement, and §4.11 is
explicit that unfinished work is flagged off rather than faked.

**The real Enron corpus is not wired to anything here.** Pointing `--root` at it is a
deliberate act by an operator who has downloaded it, and §19's sampling belongs to
`sample_enron` rather than to this command.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

from jutsu_db.engine import org_session
from jutsu_retrieval.client import VertexTransport
from jutsu_retrieval.config import get_embedding_settings
from jutsu_retrieval.embeddings import Embedder
from sqlalchemy import text

from jutsu_worker.ingest import source_job_key
from jutsu_worker.jobs import JobKind, enqueue_job
from jutsu_worker.runner import process_document, process_embedding, process_source

logger = logging.getLogger("jutsu.worker.cli")

#: A bound on one seed run, so a mistyped `--root` cannot start an unbounded job. It is
#: deliberately low: §20's budget guardrails are about not discovering a spend after the
#: fact, and a corpus-wide run is a decision, not a default.
DEFAULT_MAX_DOCUMENTS = 500


async def ensure_source(org_id: uuid.UUID, root: str) -> uuid.UUID:
    """The `local` source for this corpus, created once and reused.

    Keyed on the resolved path so re-seeding the same directory reuses the same source
    row — and therefore the same cursor and the same document identities. A fresh source
    each run would make every document look new, which is the failure this whole slice
    exists to prevent.
    """
    config = json.dumps({"root": root})
    async with org_session(org_id) as session:
        existing = (
            await session.execute(
                text(
                    "SELECT id FROM sources WHERE system = 'local' "
                    "AND config_json ->> 'root' = :root"
                ),
                {"root": root},
            )
        ).scalar_one_or_none()
        if existing is not None:
            return uuid.UUID(str(existing))

        source_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json) "
                "VALUES (:id, :org, 'local', CAST(:config AS jsonb))"
            ),
            {"id": str(source_id), "org": str(org_id), "config": config},
        )
        return source_id


async def seed(
    org_id: uuid.UUID, root: str, *, embed: bool, max_documents: int = DEFAULT_MAX_DOCUMENTS
) -> int:
    """Walk the corpus, ingest every document, optionally embed. Returns documents handled.

    Embedding is opt-in because it is the only stage that spends money. `--embed` builds a
    real Vertex transport and therefore a real bill; without it the corpus lands in
    Postgres with `embedding IS NULL`, and a later run picks exactly those chunks up.

    `root` arrives already resolved and already checked. Validating a path is filesystem
    work and belongs at the command boundary, not inside the async path where it blocks
    the loop for every caller that reuses this function.
    """
    source_id = await ensure_source(org_id, root)

    async with org_session(org_id) as session:
        await enqueue_job(
            session,
            org_id=org_id,
            kind=JobKind.INGEST_SOURCE,
            idempotency_key=source_job_key(org_id, source_id),
            payload={"source_id": str(source_id)},
        )
    # A previous seed leaves that job `completed`, so re-running needs it claimable again.
    # This is the one place a state is reset by hand, and it is what "run the sync again"
    # means for a command with no scheduler behind it yet.
    async with org_session(org_id) as session:
        await session.execute(
            text(
                "UPDATE jobs SET state = 'pending', locked_until = NULL, next_attempt_at = NULL "
                "WHERE kind = :kind AND idempotency_key = :key"
            ),
            {"kind": JobKind.INGEST_SOURCE.value, "key": source_job_key(org_id, source_id)},
        )

    await process_source(org_id, source_id, correlation_id="seed")

    handled = 0
    for _ in range(max_documents):
        if await process_document(org_id) is None:
            break
        handled += 1

    if embed:
        settings = get_embedding_settings()
        transport = VertexTransport(settings)
        try:
            embedder = Embedder(transport, settings)
            for _ in range(max_documents):
                if await process_embedding(org_id, embedder) is None:
                    break
        finally:
            # `VertexTransport` exposes `aclose` rather than the context-manager protocol,
            # and an unclosed httpx client leaks a connection pool per run.
            await transport.aclose()

    logger.info("seed_completed org=%s source=%s documents=%d", org_id, source_id, handled)
    return handled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jutsu-seed", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("seed", help="ingest a local corpus, idempotently")
    run.add_argument("--org", required=True, help="organisation id to ingest into")
    run.add_argument("--root", required=True, type=Path, help="corpus directory")
    run.add_argument(
        "--embed",
        action="store_true",
        help="also embed the chunks — this calls the provider and costs money",
    )
    run.add_argument("--max-documents", type=int, default=DEFAULT_MAX_DOCUMENTS)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Resolved and checked here, synchronously, before any connection is opened. A corpus
    # that is not there should fail in the first millisecond rather than after a migration
    # check and a connection pool.
    root = Path(args.root).resolve()
    if not root.is_dir():
        raise SystemExit(f"corpus root does not exist: {root}")

    handled = asyncio.run(
        seed(
            uuid.UUID(args.org),
            str(root),
            embed=args.embed,
            max_documents=args.max_documents,
        )
    )
    # Printed rather than logged: this is the command's answer, and a second run printing
    # 0 is the idempotency claim being demonstrated rather than asserted.
    sys.stdout.write(f"documents handled: {handled}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
