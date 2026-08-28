"""Seed receipts — what they must record, and what they must not.

M1's last clause is satisfied by a file existing with the right numbers in it, so these
tests are mostly about the two ways that goes wrong: a receipt that omits the figure it
was written for, and a receipt that records more than it should.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from jutsu_evals.receipts import (
    RECEIPT_VERSION,
    SeedReceipt,
    latest_receipt,
    receipts_dir,
    root_hash,
    write_receipt,
)

CORPUS = "/home/ritik/corpora/enron/maildir"


def _receipt(
    org_id: uuid.UUID,
    *,
    finished: str = "2026-08-28T12:00:00+00:00",
    documents: int = 47_102,
    tokens: int = 1_284_991,
    embedded: bool = True,
) -> SeedReceipt:
    started = datetime.fromisoformat(finished).replace(hour=11)
    return SeedReceipt.build(
        org_id=org_id,
        root=CORPUS,
        started_at=started,
        finished_at=datetime.fromisoformat(finished),
        documents=documents,
        chunks=documents * 3,
        embedded=embedded,
        tokens=tokens,
        requests=812,
        model="gemini-embedding-001",
        dimension=768,
    )


class TestWhatAReceiptRecords:
    def test_it_carries_every_figure_m1_asks_for(self) -> None:
        receipt = _receipt(uuid.uuid4())
        assert receipt.documents == 47_102
        assert receipt.chunks == 47_102 * 3
        assert receipt.tokens == 1_284_991
        assert receipt.model == "gemini-embedding-001"
        assert receipt.dimension == 768
        assert receipt.elapsed_seconds == 3600.0

    def test_elapsed_is_computed_not_supplied(self) -> None:
        """Two clocks would eventually disagree; there is one subtraction."""
        started = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)
        receipt = SeedReceipt.build(
            org_id=uuid.uuid4(),
            root=CORPUS,
            started_at=started,
            finished_at=datetime(2026, 8, 28, 12, 1, 30, tzinfo=UTC),
            documents=1,
            chunks=2,
            embedded=False,
            tokens=0,
            requests=0,
            model=None,
            dimension=None,
        )
        assert receipt.elapsed_seconds == 90.0

    def test_a_run_without_embedding_records_zero_rather_than_nothing(self) -> None:
        receipt = _receipt(uuid.uuid4(), embedded=False, tokens=0)
        assert receipt.embedded is False
        assert receipt.tokens == 0


class TestWhatAReceiptMustNotRecord:
    def test_the_corpus_path_never_reaches_the_file(self, tmp_path: Path) -> None:
        """A local corpus path contains an account name. §4.9 has no file carve-out."""
        org_id = uuid.uuid4()
        path = write_receipt(tmp_path, _receipt(org_id))
        written = path.read_text(encoding="utf-8")

        assert CORPUS not in written
        assert "ritik" not in written
        assert root_hash(CORPUS) in written

    def test_the_hash_still_identifies_the_same_corpus(self) -> None:
        assert root_hash(CORPUS) == root_hash(CORPUS)
        assert root_hash(CORPUS) != root_hash(CORPUS + "/2")


class TestReadingReceiptsBack:
    def test_a_round_trip_preserves_every_field(self, tmp_path: Path) -> None:
        org_id = uuid.uuid4()
        original = _receipt(org_id)
        write_receipt(tmp_path, original)
        assert latest_receipt(tmp_path, org_id) == original

    def test_the_most_recent_receipt_wins(self, tmp_path: Path) -> None:
        org_id = uuid.uuid4()
        write_receipt(tmp_path, _receipt(org_id, finished="2026-08-27T12:00:00+00:00", tokens=1))
        write_receipt(tmp_path, _receipt(org_id, finished="2026-08-28T12:00:00+00:00", tokens=2))
        found = latest_receipt(tmp_path, org_id)
        assert found is not None
        assert found.tokens == 2

    def test_another_organisation_is_not_returned(self, tmp_path: Path) -> None:
        write_receipt(tmp_path, _receipt(uuid.uuid4()))
        assert latest_receipt(tmp_path, uuid.uuid4()) is None

    def test_no_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert latest_receipt(tmp_path / "nothing-here", uuid.uuid4()) is None

    def test_an_unknown_version_is_ignored_rather_than_guessed_at(self, tmp_path: Path) -> None:
        org_id = uuid.uuid4()
        payload = _receipt(org_id).to_dict()
        payload["version"] = RECEIPT_VERSION + 1
        directory = receipts_dir(tmp_path)
        directory.mkdir(parents=True)
        (directory / "seed-2026.json").write_text(json.dumps(payload), encoding="utf-8")

        assert latest_receipt(tmp_path, org_id) is None

    def test_a_half_written_receipt_does_not_take_the_gate_down(self, tmp_path: Path) -> None:
        """An interrupted run leaves truncated JSON. The gate must survive it."""
        org_id = uuid.uuid4()
        write_receipt(tmp_path, _receipt(org_id))
        directory = receipts_dir(tmp_path)
        (directory / "seed-truncated.json").write_text('{"version": 1, "org', encoding="utf-8")

        assert latest_receipt(tmp_path, org_id) is not None

    def test_the_filename_has_no_colon(self, tmp_path: Path) -> None:
        """Windows refuses one, and a receipt that silently fails to write is worse."""
        path = write_receipt(tmp_path, _receipt(uuid.uuid4()))
        assert ":" not in path.name
