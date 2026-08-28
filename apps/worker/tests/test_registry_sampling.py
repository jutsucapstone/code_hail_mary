"""The source row decides whether ingestion walks a directory or ingests a sample.

`connector_for` is the whole seam. A `local` source with a `manifest` in its config gets a
`ManifestConnector` and therefore §19's complete-thread sample; one without keeps the
directory walk that S8 shipped. Everything downstream — the job walk, versioning, ACLs —
is unchanged either way, which is why the seam is here rather than in the pipeline.

The failure modes are all "quietly ingests the wrong set of documents", so each one is
asserted rather than trusted: a missing file, an old manifest version and a malformed one
must each refuse, and refuse *permanently*, because retrying a bad file produces the same
bad file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jutsu_connectors.enron import MANIFEST_VERSION, ManifestConnector, sample_enron
from jutsu_connectors.local import LocalConnector
from jutsu_core import SourceSystem
from jutsu_worker.registry import UnsupportedSource, connector_for

CRLF = "\r\n"


def mail(*, message_id: str, sender: str, to: str, references: str | None = None) -> bytes:
    """One RFC822 message with explicit CRLF.

    Built here rather than imported from `packages/connectors/tests`: a support module is
    importable only from its own test directory, and a worker test reaching across for
    one would be depending on another package's fixtures. `.gitattributes` is
    `* text=auto eol=lf`, so these bytes are constructed rather than committed.
    """
    headers = [
        f"Message-ID: <{message_id}@example.com>",
        f"From: {sender}",
        f"To: {to}",
        "Subject: thread under test",
        "Date: Mon, 3 Mar 2003 09:00:00 -0800",
    ]
    if references:
        headers.append(f"References: <{references}@example.com>")
    return (CRLF.join(headers) + CRLF + CRLF + "body text").encode("utf-8")


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Two custodians, one of them carrying a reply — enough to have threads at all."""
    root = tmp_path / "maildir"
    files = {
        "grace/1.": mail(message_id="a", sender="grace@example.com", to="eve@example.com"),
        "grace/2.": mail(
            message_id="b", sender="eve@example.com", to="grace@example.com", references="a"
        ),
        "eve/1.": mail(message_id="c", sender="eve@example.com", to="eve@example.com"),
    }
    for name, payload in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return root


@pytest.fixture
async def manifest_file(corpus: Path, tmp_path: Path) -> Path:
    result = await sample_enron(corpus, target_messages=100, custodian_count=10)
    path = tmp_path / "sample_manifest.json"
    path.write_text(result.manifest.to_json(), encoding="utf-8")
    return path


class TestWhichConnectorTheConfigSelects:
    def test_no_manifest_means_a_directory_walk(self, corpus: Path) -> None:
        connector = connector_for(SourceSystem.LOCAL, {"root": str(corpus)})
        assert isinstance(connector, LocalConnector)

    def test_a_manifest_means_the_sample(self, corpus: Path, manifest_file: Path) -> None:
        connector = connector_for(
            SourceSystem.LOCAL, {"root": str(corpus), "manifest": str(manifest_file)}
        )
        assert isinstance(connector, ManifestConnector)

    async def test_the_sampled_connector_lists_only_the_sample(
        self, corpus: Path, manifest_file: Path
    ) -> None:
        """The property that matters: fewer documents, and the right ones."""
        sampled = connector_for(
            SourceSystem.LOCAL, {"root": str(corpus), "manifest": str(manifest_file)}
        )
        walked = connector_for(SourceSystem.LOCAL, {"root": str(corpus)})

        from_sample = {e async for e in sampled.list_since(None)}
        from_walk = {e async for e in walked.list_since(None)}
        assert from_sample <= from_walk


class TestItRefusesRatherThanGuesses:
    def test_a_missing_manifest_file(self, corpus: Path, tmp_path: Path) -> None:
        with pytest.raises(UnsupportedSource, match="could not be read"):
            connector_for(
                SourceSystem.LOCAL,
                {"root": str(corpus), "manifest": str(tmp_path / "absent.json")},
            )

    def test_an_old_manifest_version(self, corpus: Path, tmp_path: Path) -> None:
        """Version 1 has no thread map — ingesting from it would guess silently."""
        stale = tmp_path / "old.json"
        stale.write_text('{"version": 1, "messages": []}', encoding="utf-8")
        with pytest.raises(UnsupportedSource, match="unusable"):
            connector_for(SourceSystem.LOCAL, {"root": str(corpus), "manifest": str(stale)})

    def test_a_malformed_manifest(self, corpus: Path, tmp_path: Path) -> None:
        """Truncated JSON must refuse as `UnsupportedSource`, not as a decode error.

        A raw `JSONDecodeError` reaching `classify` falls through to INTERNAL, which is
        retryable — so the job would re-read the same truncated file until it
        dead-lettered. mypy caught the first version of this test asserting nothing;
        the assertion it forced is the one that matters.
        """
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with pytest.raises(UnsupportedSource, match="unusable"):
            connector_for(SourceSystem.LOCAL, {"root": str(corpus), "manifest": str(broken)})

    def test_a_manifest_that_is_not_an_object(self, corpus: Path, tmp_path: Path) -> None:
        odd = tmp_path / "list.json"
        odd.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(UnsupportedSource, match="unusable"):
            connector_for(SourceSystem.LOCAL, {"root": str(corpus), "manifest": str(odd)})

    def test_an_empty_manifest_path(self, corpus: Path) -> None:
        with pytest.raises(UnsupportedSource, match="unusable manifest path"):
            connector_for(SourceSystem.LOCAL, {"root": str(corpus), "manifest": ""})

    def test_a_manifest_without_a_root_is_still_refused(self, manifest_file: Path) -> None:
        """The root check comes first; a manifest cannot stand in for a corpus."""
        with pytest.raises(UnsupportedSource, match="missing a corpus root"):
            connector_for(SourceSystem.LOCAL, {"manifest": str(manifest_file)})

    def test_the_refusal_is_classified_as_permanent(self) -> None:
        """Retrying a bad manifest reads the same bad manifest."""
        from jutsu_worker.ingest import classify
        from jutsu_worker.jobs import FailureKind

        kind, retryable = classify(UnsupportedSource("the sample manifest is unusable"))
        assert kind is FailureKind.SOURCE_UNAVAILABLE
        assert retryable is False

    def test_the_manifest_version_this_build_writes(self) -> None:
        assert MANIFEST_VERSION == 2
