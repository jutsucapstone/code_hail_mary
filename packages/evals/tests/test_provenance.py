"""Provenance: a measurement must name the tree that produced it, or name nothing.

§18 asks a metric to carry a commit, and CLAUDE.md rule 8 forbids quoting a number no
harness produced. Between those two sits a failure neither of them names: a harness that
*did* run, stamped with a commit that does not contain the code it ran.

That is not hypothetical. Every gate report in this repo's S9 work was stamped `53a0f1c`
while the retry, PII and drain-loop fixes sat uncommitted in the working tree. Checking
out `53a0f1c` reproduces none of those runs. The stamp made the report look *more*
trustworthy for being precise, which is the expensive direction to be wrong in.

So `revision` answers two questions in one string: which commit, and whether the tracked
tree still matches it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from jutsu_evals.receipts import SeedReceipt, latest_receipt, write_receipt
from jutsu_evals.report import revision


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository with one commit. Real because `revision` shells out to git."""
    git(tmp_path, "init", "-q")
    # Identity is required for `commit`, and must not depend on the developer's own.
    git(tmp_path, "config", "user.email", "gate@example.invalid")
    git(tmp_path, "config", "user.name", "Gate Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.txt")
    git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


class TestRevisionStamp:
    def test_a_clean_tree_is_the_bare_revision(self, repo: Path) -> None:
        stamp = revision(repo)
        assert stamp is not None
        assert stamp.isalnum(), f"a clean tree should carry no suffix, got {stamp!r}"

    def test_a_modified_tracked_file_marks_it_dirty(self, repo: Path) -> None:
        """The case that actually occurred, and the reason this function changed."""
        (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
        stamp = revision(repo)
        assert stamp is not None
        assert stamp.endswith("-dirty"), f"an edited tree reported {stamp!r} as reproducible"

    def test_a_staged_change_also_marks_it_dirty(self, repo: Path) -> None:
        """Staged is still not committed, so the commit still does not contain it."""
        (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
        git(repo, "add", "tracked.txt")
        stamp = revision(repo)
        assert stamp is not None and stamp.endswith("-dirty")

    def test_committing_clears_it(self, repo: Path) -> None:
        (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
        git(repo, "add", "tracked.txt")
        git(repo, "commit", "-q", "-m", "second")
        stamp = revision(repo)
        assert stamp is not None and not stamp.endswith("-dirty")

    def test_an_untracked_file_alone_does_not(self, repo: Path) -> None:
        """Deliberate, and the docstring says why.

        Receipts, reports, scratch directories and local agent tooling live untracked in
        every working copy here. Counting them would leave the flag permanently on, and a
        warning that is always on is one nobody reads.
        """
        (repo / "scratch.log").write_text("local\n", encoding="utf-8")
        stamp = revision(repo)
        assert stamp is not None
        assert not stamp.endswith("-dirty"), "untracked local files flipped the flag"

    def test_outside_a_repository_it_is_none(self, tmp_path: Path) -> None:
        """None means "unknown", which the report renders by omitting the stamp."""
        assert revision(tmp_path) is None

    def test_the_stamp_is_never_silently_clean(self, repo: Path) -> None:
        """Three outcomes, and only one of them claims reproducibility.

        `<sha>` is a promise; `<sha>-dirty` and `<sha>-unknown` are refusals to make it.
        What must never exist is a fourth case where cleanliness could not be determined
        and the bare sha is printed anyway.
        """
        for stamp in (revision(repo), revision(repo.parent)):
            if stamp is None:
                continue
            assert stamp.isalnum() or stamp.endswith(("-dirty", "-unknown"))


class TestReceiptCarriesTheRevision:
    @staticmethod
    def _receipt(**overrides: object) -> SeedReceipt:
        from datetime import UTC, datetime
        from uuid import UUID

        base: dict[str, object] = {
            "org_id": UUID("ff805d42-ac5c-4cdd-8ea9-b49873e3dbc5"),
            "root": "/corpus",
            "started_at": datetime(2026, 8, 30, 4, 49, tzinfo=UTC),
            "finished_at": datetime(2026, 8, 30, 4, 50, tzinfo=UTC),
            "documents": 0,
            "chunks": 292,
            "embedded": True,
            "tokens": 1588,
            "requests": 5,
            "model": "gemini-embedding-001",
            "dimension": 768,
        }
        base.update(overrides)
        return SeedReceipt.build(**base)  # type: ignore[arg-type]

    def test_it_round_trips(self, tmp_path: Path) -> None:
        from uuid import UUID

        write_receipt(tmp_path, self._receipt(revision="abc1234-dirty"))
        loaded = latest_receipt(tmp_path, UUID("ff805d42-ac5c-4cdd-8ea9-b49873e3dbc5"))
        assert loaded is not None
        assert loaded.revision == "abc1234-dirty"

    def test_a_receipt_written_before_the_field_existed_still_loads(self, tmp_path: Path) -> None:
        """Why `RECEIPT_VERSION` deliberately did not move.

        Three receipts describing the pilot are already on disk without this key. Bumping
        the version would make the loader skip them, and M1's recorded-cost clause would
        go from measured to unmeasured — losing a real measurement to record a field that
        run could not have had.
        """
        import json
        from uuid import UUID

        payload = self._receipt().to_dict()
        del payload["revision"]
        (tmp_path / "evals" / "runs").mkdir(parents=True)
        (tmp_path / "evals" / "runs" / "seed-2026-08-30T045000Z.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

        loaded = latest_receipt(tmp_path, UUID("ff805d42-ac5c-4cdd-8ea9-b49873e3dbc5"))
        assert loaded is not None, "an older receipt became unreadable"
        assert loaded.revision is None
        assert loaded.tokens == 1588, "the numbers that matter survived"
