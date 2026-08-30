"""What one seed run cost, written down (§21 M1, §20).

M1's last clause is "seed-run token cost recorded". Before this slice the figure existed
— `Embedder` keeps a `TokenLedger` and charges the provider's own token counts to it —
and then the process exited and it was gone. `make seed` logged a document count and
nothing else, so the only honest answer to "what did the seed cost" was "nobody wrote it
down", which under rule 8 means the clause is unmeasurable rather than free.

A receipt is one JSON file per run under `evals/runs/`, gitignored: it describes a local
run over a local corpus and is evidence for a gate, not a project artefact. The *report*
the gate produces from it is the artefact, and that is committed.

**A receipt stores no filesystem path.** `root_hash` identifies the corpus well enough to
say "the same directory as last time" and reveals nothing; the resolved path of a corpus
on a developer's machine contains their account name, and §4.9 does not carve out an
exception for files that happen not to be called logs. The captured log is likewise
passed to the gate on the command line rather than recorded here.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

__all__ = [
    "RECEIPT_VERSION",
    "SeedReceipt",
    "latest_receipt",
    "receipts_dir",
    "root_hash",
    "write_receipt",
]

#: Bumped when a field changes meaning. A gate reading a receipt it does not understand
#: reports "not measured" rather than guessing at an older shape.
RECEIPT_VERSION = 1


def receipts_dir(repo_root: Path) -> Path:
    return repo_root / "evals" / "runs"


def root_hash(root: str) -> str:
    """Stable identifier for a corpus directory that is not the directory's name."""
    return sha256(root.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SeedReceipt:
    """One `make seed` run, in numbers.

    `tokens` is the provider's own figure via `TokenLedger`, never an estimate — the
    same distinction `Embedding.token_count` already makes. On a run without `--embed`
    it is 0, which is a recorded cost rather than a missing one: that run genuinely
    spent nothing, and the receipt says so with `embedded: false`.
    """

    version: int
    org_id: str
    root_hash: str
    started_at: str
    finished_at: str
    elapsed_seconds: float
    documents: int
    chunks: int
    embedded: bool
    tokens: int
    requests: int
    model: str | None
    dimension: int | None
    #: Short git revision of the tree that produced this run, `-dirty` when tracked
    #: files differed from it. Optional, and `RECEIPT_VERSION` deliberately does NOT
    #: move for it: the version exists to reject a shape that would be *misread*, and
    #: an added optional key is not one. Bumping it would make the three receipts
    #: already on disk unreadable and silently turn M1's recorded-cost clause from
    #: measured into unmeasured — losing the pilot's cost record to gain nothing.
    revision: str | None = None

    @staticmethod
    def build(
        *,
        org_id: uuid.UUID,
        root: str,
        started_at: datetime,
        finished_at: datetime,
        documents: int,
        chunks: int,
        embedded: bool,
        tokens: int,
        requests: int,
        model: str | None,
        dimension: int | None,
        revision: str | None = None,
    ) -> SeedReceipt:
        return SeedReceipt(
            version=RECEIPT_VERSION,
            org_id=str(org_id),
            root_hash=root_hash(root),
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            elapsed_seconds=round((finished_at - started_at).total_seconds(), 3),
            documents=documents,
            chunks=chunks,
            embedded=embedded,
            tokens=tokens,
            requests=requests,
            model=model,
            dimension=dimension,
            revision=revision,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def write_receipt(repo_root: Path, receipt: SeedReceipt) -> Path:
    """Write one run's receipt and return where it landed.

    Named by finish time in UTC with colons stripped, because Windows refuses a filename
    containing one and a receipt that silently fails to write on half the team's machines
    is worse than no receipt at all.
    """
    directory = receipts_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = receipt.finished_at.replace(":", "").replace("+0000", "Z")
    path = directory / f"seed-{stamp}.json"
    path.write_text(json.dumps(receipt.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path


def latest_receipt(repo_root: Path, org_id: uuid.UUID) -> SeedReceipt | None:
    """The most recent readable receipt for one organisation, or None.

    Unreadable and unrecognised-version files are skipped rather than raising: a
    half-written receipt from an interrupted run must not take the gate down with it.
    """
    directory = receipts_dir(repo_root)
    if not directory.is_dir():
        return None

    best: tuple[str, SeedReceipt] | None = None
    for path in sorted(directory.glob("seed-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("version") != RECEIPT_VERSION:
            continue
        if payload.get("org_id") != str(org_id):
            continue
        try:
            receipt = SeedReceipt(**payload)
        except TypeError:
            continue
        if best is None or receipt.finished_at > best[0]:
            best = (receipt.finished_at, receipt)

    return best[1] if best else None


def utcnow() -> datetime:
    """One place, so a receipt's two timestamps come from the same clock."""
    return datetime.now(UTC)
