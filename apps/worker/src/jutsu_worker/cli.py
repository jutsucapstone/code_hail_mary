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

**`--sample` is how the real Enron corpus is ingested, and on that corpus it is not
optional.** Without it this command walks the directory and takes whatever it meets up to
`--max-documents` — which is a *directory walk*, and on a 500k-message maildir that is
precisely the random sampling §19 forbids. It shreds the reply graph, leaves entity
resolution with nothing to resolve, and the symptom appears weeks later somewhere that
looks unrelated.

`--sample` runs `sample_enron` first, writes `sample_manifest.json` beside the corpus, and
records it on the source row. Every subsequent seed of that source ingests the sampled
identifiers and nothing else, with the thread ids the whole-corpus index assigned.
Downloading the corpus is still a deliberate act by an operator; this command never
fetches one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

from jutsu_connectors.enron import (
    DEFAULT_CUSTODIAN_COUNT,
    DEFAULT_SEED,
    DEFAULT_TARGET_MESSAGES,
    sample_enron,
)
from jutsu_db.engine import org_session
from jutsu_evals.receipts import SeedReceipt, utcnow, write_receipt
from jutsu_evals.report import revision
from jutsu_retrieval.client import VertexTransport
from jutsu_retrieval.config import MissingEmbeddingSettings, get_embedding_settings
from jutsu_retrieval.embeddings import Embedder, TokenLedger
from sqlalchemy import text

from jutsu_worker.ingest import source_job_key
from jutsu_worker.jobs import JobKind, enqueue_job
from jutsu_worker.runner import JOB_FAILED, process_document, process_embedding, process_source

logger = logging.getLogger("jutsu.worker.cli")

#: Where `evals/runs/` lives. The workspace is installed editable, so this file sits at
#: `<repo>/apps/worker/src/jutsu_worker/cli.py` and the fourth parent is the root.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: A bound on one seed run, so a mistyped `--root` cannot start an unbounded job. It is
#: deliberately low: §20's budget guardrails are about not discovering a spend after the
#: fact, and a corpus-wide run is a decision, not a default.
DEFAULT_MAX_DOCUMENTS = 500


async def ensure_source(org_id: uuid.UUID, root: str, manifest: str | None = None) -> uuid.UUID:
    """The `local` source for this corpus, created once and reused.

    Keyed on the resolved path so re-seeding the same directory reuses the same source
    row — and therefore the same cursor and the same document identities. A fresh source
    each run would make every document look new, which is the failure this whole slice
    exists to prevent.

    `manifest` switches the source from a directory walk to §19's thread sample. It is
    written onto an existing row as well as a new one: the corpus is the same corpus, so
    the identities and the cursor must be kept, and creating a second source for the
    sampled view would re-ingest every document as new.
    """
    payload: dict[str, str] = {"root": root}
    if manifest is not None:
        payload["manifest"] = manifest
    config = json.dumps(payload)

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
            source_id = uuid.UUID(str(existing))
            await session.execute(
                text("UPDATE sources SET config_json = CAST(:config AS jsonb) WHERE id = :id"),
                {"id": str(source_id), "config": config},
            )
            return source_id

        source_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json) "
                "VALUES (:id, :org, 'local', CAST(:config AS jsonb))"
            ),
            {"id": str(source_id), "org": str(org_id), "config": config},
        )
        return source_id


async def count_current_chunks(org_id: uuid.UUID) -> int:
    """Chunks belonging to current-version documents in this organisation.

    For the run receipt. Org-scoped through `org_session` like everything else, so the
    figure recorded is the one the application can actually see — a count taken through
    a privileged connection would describe a different database from the one the gate
    later measures.
    """
    async with org_session(org_id) as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
                        "WHERE d.superseded_by IS NULL"
                    )
                )
            ).scalar_one()
        )


async def seed(
    org_id: uuid.UUID,
    root: str,
    *,
    embed: bool,
    max_documents: int = DEFAULT_MAX_DOCUMENTS,
    max_embed_jobs: int | None = None,
    ledger: TokenLedger | None = None,
    manifest: str | None = None,
) -> int:
    """Walk the corpus, ingest every document, optionally embed. Returns documents handled.

    Embedding is opt-in because it is the only stage that spends money. `--embed` builds a
    real Vertex transport and therefore a real bill; without it the corpus lands in
    Postgres with `embedding IS NULL`, and a later run picks exactly those chunks up.

    `root` arrives already resolved and already checked. Validating a path is filesystem
    work and belongs at the command boundary, not inside the async path where it blocks
    the loop for every caller that reuses this function.

    `ledger` lets the caller keep the token account after this returns. `Embedder` builds
    its own when none is passed, which is what every existing caller relies on — but a
    ledger that only ever lives inside this function is a cost that is spent and then
    forgotten, which is exactly why M1's last clause was unmeasurable. Passing one in is
    additive: the ingestion path is unchanged either way.
    """
    source_id = await ensure_source(org_id, root, manifest)

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

    # A failed job is skipped, not treated as the end of the queue.
    #
    # Both loops used to `break` on `None`, and `None` meant both "nothing claimable" and
    # "this job failed". The 200-document pilot proved what that costs: one HTTP 429 on
    # the thirteenth embedding job ended the whole phase, leaving 187 jobs untouched, and
    # the command still exited 0. Only an empty queue ends a drain now; a failure is
    # already recorded on its row, so the loop moves on.
    handled = 0
    for _ in range(max_documents):
        outcome = await process_document(org_id)
        if outcome is None:
            break
        if outcome is JOB_FAILED:
            continue
        handled += 1

    if embed:
        # `max_documents` bounds the *ingest* loop. Reusing it for the embedding loop was
        # a coincidence of the two loops being written together, not a contract - and it
        # is the wrong bound, because the two stages do not cost the same thing. Ingest is
        # local IO; embedding is a metered provider call. An operator resuming a stalled
        # queue wants to say "spend five jobs' worth", and saying it through a document
        # limit only works while every document is also exactly one embedding job.
        #
        # `None` keeps the old behaviour exactly, so no existing caller or Makefile
        # target changes meaning.
        embed_budget = max_documents if max_embed_jobs is None else max_embed_jobs
        settings = get_embedding_settings()
        transport = VertexTransport(settings)
        try:
            embedder = Embedder(transport, settings, ledger=ledger)
            for _ in range(embed_budget):
                result = await process_embedding(org_id, embedder)
                if result is None:
                    break
                if result is JOB_FAILED:
                    # Recorded and either rescheduled or dead-lettered. The next job is
                    # unrelated to this one's provider error, so keep draining.
                    continue
        finally:
            # `VertexTransport` exposes `aclose` rather than the context-manager protocol,
            # and an unclosed httpx client leaks a connection pool per run.
            await transport.aclose()

    logger.info("seed_completed org=%s source=%s documents=%d", org_id, source_id, handled)
    return handled


async def build_sample(
    root: Path, *, seed_value: int, target_messages: int, custodian_count: int
) -> Path:
    """Run §19's sampler over the corpus and write the manifest. Returns its path.

    Reads headers only — that is what makes half a million messages tractable — and
    writes `sample_manifest.json` beside the corpus rather than into `evals/runs/`,
    because it describes *that corpus* and belongs with it. The file is the input to
    every subsequent seed of this source, so re-running the sampler with the same seed
    is a no-op on the bytes and re-running it with a different one is a visible change.

    Nothing is ingested here. Selection and persistence stay separate, which is the
    division ADR 0008 already drew.
    """
    result = await sample_enron(
        root,
        seed=seed_value,
        target_messages=target_messages,
        custodian_count=custodian_count,
    )
    manifest_path = root.parent / "sample_manifest.json"
    manifest_path.write_text(result.manifest.to_json(), encoding="utf-8")

    # Counts only — no custodian names, no message ids, no addresses (§4.9).
    logger.info(
        "sample_built corpus_messages=%d sampled_messages=%d sampled_threads=%d unparsable=%d",
        result.manifest.corpus_messages,
        result.manifest.sampled_messages,
        result.manifest.sampled_threads,
        result.manifest.corpus_unparsable,
    )
    return manifest_path


async def seed_and_measure(
    org_id: uuid.UUID,
    root: str,
    *,
    embed: bool,
    max_documents: int = DEFAULT_MAX_DOCUMENTS,
    max_embed_jobs: int | None = None,
    manifest: str | None = None,
) -> SeedReceipt:
    """Run the seed and record what it cost (§21 M1, "seed-run token cost recorded").

    The ledger has to exist before the run, because `Embedder` charges into it as it
    goes and its budget is what stops a retry loop re-embedding a corpus. Reading the
    settings early would otherwise move *when* a misconfigured `--embed` run fails —
    today it ingests first and raises afterwards — so a missing configuration is caught
    and dropped here, leaving `seed` to raise it at exactly the point it always has.

    Nothing about ingestion changes. This wraps the run, times it, and writes down the
    numbers that were previously discarded at process exit.
    """
    ledger: TokenLedger | None = None
    model: str | None = None
    dimension: int | None = None

    if embed:
        try:
            settings = get_embedding_settings()
        except MissingEmbeddingSettings:
            settings = None
        if settings is not None:
            ledger = TokenLedger(budget=settings.token_budget)
            model, dimension = settings.model, settings.dimensions

    started = utcnow()
    documents = await seed(
        org_id,
        root,
        embed=embed,
        max_documents=max_documents,
        max_embed_jobs=max_embed_jobs,
        ledger=ledger,
        manifest=manifest,
    )
    chunks = await count_current_chunks(org_id)
    finished = utcnow()

    return SeedReceipt.build(
        org_id=org_id,
        root=root,
        started_at=started,
        finished_at=finished,
        documents=documents,
        chunks=chunks,
        embedded=embed,
        # Zero on a run without `--embed`: a recorded cost of nothing, not a missing one.
        tokens=ledger.spent if ledger else 0,
        requests=ledger.requests if ledger else 0,
        model=model,
        dimension=dimension,
        # Which tree produced these numbers. `-dirty` when tracked files differ from the
        # commit, because a receipt naming a commit that does not contain the code is a
        # reproducibility claim nobody can honour.
        revision=revision(REPO_ROOT),
    )


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
    run.add_argument(
        "--max-embed-jobs",
        type=int,
        default=None,
        help=(
            "how many embedding jobs to drain, separately from --max-documents. "
            "Each one is a provider call and costs money. Defaults to --max-documents, "
            "which is what this command did before the flag existed."
        ),
    )
    run.add_argument(
        "--log-file",
        type=Path,
        help="also write this run's log here, so `scripts/gate.py --log` can scan it for PII",
    )
    run.add_argument(
        "--no-receipt", action="store_true", help="do not write a run receipt to evals/runs/"
    )
    run.add_argument(
        "--sample",
        action="store_true",
        help="sample complete threads first (§19) and ingest only those — required for Enron",
    )
    run.add_argument("--sample-seed", type=int, default=DEFAULT_SEED)
    run.add_argument("--sample-target", type=int, default=DEFAULT_TARGET_MESSAGES)
    run.add_argument("--sample-custodians", type=int, default=DEFAULT_CUSTODIAN_COUNT)
    run.add_argument(
        "--manifest",
        type=Path,
        help="use an existing sample manifest instead of building one",
    )

    args = parser.parse_args(argv)

    # A file handler alongside the stream one, not instead of it. The gate's PII clause
    # is about the log this run actually emitted, so the captured copy has to be the
    # same lines at the same level — a quieter file would be a check that passes because
    # it was shown less.
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=handlers)

    # Resolved and checked here, synchronously, before any connection is opened. A corpus
    # that is not there should fail in the first millisecond rather than after a migration
    # check and a connection pool.
    root = Path(args.root).resolve()
    if not root.is_dir():
        raise SystemExit(f"corpus root does not exist: {root}")

    if args.sample and args.manifest:
        raise SystemExit("--sample builds a manifest and --manifest supplies one; pick one")

    # `range(-1)` is empty, so a mistyped limit would embed nothing, print a cheerful
    # "tokens spent: 0" and exit 0 - a resume that silently did not happen.
    if args.max_embed_jobs is not None and args.max_embed_jobs < 0:
        raise SystemExit("--max-embed-jobs cannot be negative")

    manifest: str | None = None
    if args.manifest:
        manifest_path = Path(args.manifest).resolve()
        if not manifest_path.is_file():
            raise SystemExit(f"manifest does not exist: {manifest_path}")
        manifest = str(manifest_path)
    elif args.sample:
        # Runs before any connection is opened, like the root check above: sampling a
        # 500k-message corpus is minutes of header parsing, and discovering a bad
        # `--root` afterwards would waste all of it.
        manifest = str(
            asyncio.run(
                build_sample(
                    root,
                    seed_value=args.sample_seed,
                    target_messages=args.sample_target,
                    custodian_count=args.sample_custodians,
                )
            )
        )
        sys.stdout.write(f"sample manifest: {manifest}\n")

    receipt = asyncio.run(
        seed_and_measure(
            uuid.UUID(args.org),
            str(root),
            embed=args.embed,
            max_documents=args.max_documents,
            max_embed_jobs=args.max_embed_jobs,
            manifest=manifest,
        )
    )
    if not args.no_receipt:
        write_receipt(REPO_ROOT, receipt)

    # Printed rather than logged: this is the command's answer, and a second run printing
    # 0 is the idempotency claim being demonstrated rather than asserted.
    sys.stdout.write(f"documents handled: {receipt.documents}\n")
    sys.stdout.write(f"tokens spent: {receipt.tokens}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
