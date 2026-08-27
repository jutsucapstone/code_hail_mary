"""The local connector: protocol conformance, and path containment (§4.8, §9, §30).

Containment is the security half. `external_id` is a path, and a path that arrives from
anywhere other than this connector's own listing is untrusted input to a file read — so
the escapes are tested adversarially rather than assumed closed by construction.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from corpus_support import THREADED_CORPUS, Message, write_corpus
from jutsu_connectors.local import LocalConnector, PathEscape
from jutsu_connectors.rfc822 import UnparsableMessage
from jutsu_core import Connector, SourceSystem


@pytest.fixture(name="corpus")
def corpus_fixture(tmp_path: Path) -> Path:
    return write_corpus(tmp_path / "maildir", THREADED_CORPUS)


async def listing(connector: LocalConnector, cursor: str | None = None) -> list[str]:
    return [external_id async for external_id in connector.list_since(cursor)]


class TestProtocolConformance:
    def test_it_satisfies_the_connector_protocol(self, corpus: Path) -> None:
        """Structural, not nominal. A provider connector implements the same three."""
        assert isinstance(LocalConnector(corpus), Connector)

    def test_it_declares_its_source_system(self, corpus: Path) -> None:
        assert LocalConnector(corpus).system is SourceSystem.LOCAL

    def test_it_exposes_no_write_method(self, corpus: Path) -> None:
        """§4.8 — read-only by construction, and visibly so."""
        connector = LocalConnector(corpus)
        for forbidden in ("write", "create", "update", "delete", "put", "post", "send"):
            assert not hasattr(connector, forbidden), forbidden

    def test_a_missing_root_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            LocalConnector(tmp_path / "nope")

    def test_a_file_as_root_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "a-file"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(NotADirectoryError):
            LocalConnector(target)


class TestListing:
    async def test_it_lists_every_message(self, corpus: Path) -> None:
        assert len(await listing(LocalConnector(corpus))) == len(THREADED_CORPUS)

    async def test_identifiers_are_corpus_relative_posix_paths(self, corpus: Path) -> None:
        found = await listing(LocalConnector(corpus))
        assert "allen/sent/1" in found
        assert all(not identifier.startswith("/") for identifier in found)
        assert all("\\" not in identifier for identifier in found)

    async def test_the_order_is_deterministic(self, corpus: Path) -> None:
        """Sampling determinism starts here — `os.walk` order is not stable."""
        connector = LocalConnector(corpus)
        assert await listing(connector) == await listing(connector)
        assert await listing(connector) == sorted(await listing(connector))

    async def test_ignored_names_are_skipped(self, corpus: Path) -> None:
        (corpus / ".DS_Store").write_bytes(b"\x00")
        (corpus / "sample_manifest.json").write_text("{}", encoding="utf-8")
        found = await listing(LocalConnector(corpus))
        assert ".DS_Store" not in found
        assert "sample_manifest.json" not in found

    async def test_the_cursor_filters_by_modification_time(self, corpus: Path) -> None:
        connector = LocalConnector(corpus)
        everything = await listing(connector)
        assert everything

        future = "2999-01-01T00:00:00+00:00"
        assert await listing(connector, future) == []

        past = "1999-01-01T00:00:00+00:00"
        assert await listing(connector, past) == everything


class TestPathContainment:
    @pytest.mark.parametrize(
        "identifier",
        [
            "../outside",
            "allen/../../outside",
            "/etc/passwd",
            "allen/sent/../../../outside",
            "..",
            "",
        ],
    )
    def test_traversal_is_refused(self, corpus: Path, identifier: str) -> None:
        """Refused before any filesystem call — syntax first, then resolution."""
        with pytest.raises(PathEscape):
            LocalConnector(corpus).resolve(identifier)

    def test_a_backslash_identifier_is_refused(self, corpus: Path) -> None:
        """A separator on Windows and a legal filename character on POSIX. A rule that
        means two things is not a rule, so it is refused on both."""
        with pytest.raises(PathEscape):
            LocalConnector(corpus).resolve("allen\\sent\\1")

    def test_a_legitimate_identifier_resolves_inside_the_root(self, corpus: Path) -> None:
        connector = LocalConnector(corpus)
        resolved = connector.resolve("allen/sent/1")
        assert resolved.is_relative_to(connector.root)
        assert resolved.is_file()

    async def test_a_symlinked_file_pointing_outside_is_not_listed(self, tmp_path: Path) -> None:
        """`os.walk(followlinks=False)` governs directory recursion only.

        A symlinked *file* is walked like any other, so containment has to be re-checked
        after resolution — which is what `_relative_id` does. Skipped where the OS will
        not create symlinks without elevated privileges; CI is Linux and runs it.
        """
        secret = tmp_path / "outside.txt"
        secret.write_bytes(b"From: a@example.com\r\nSubject: secret\r\n\r\nbody\r\n")
        root = tmp_path / "maildir"
        (root / "allen").mkdir(parents=True)
        (root / "allen" / "real").write_bytes(
            b"From: b@example.com\r\nSubject: real\r\n\r\nbody\r\n"
        )
        try:
            os.symlink(secret, root / "allen" / "link")
        except (OSError, NotImplementedError) as error:
            pytest.skip(f"symlinks unavailable on this platform: {error}")

        found = await listing(LocalConnector(root))
        assert found == ["allen/real"], "a symlink escaped the corpus root"

    async def test_a_symlinked_directory_is_not_descended(self, tmp_path: Path) -> None:
        """The other half: `followlinks=False` is what stops the walk recursing out."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"From: a@example.com\r\nSubject: s\r\n\r\nb\r\n")
        root = tmp_path / "maildir"
        (root / "allen").mkdir(parents=True)
        (root / "allen" / "real").write_bytes(
            b"From: b@example.com\r\nSubject: real\r\n\r\nbody\r\n"
        )
        try:
            os.symlink(outside, root / "allen" / "elsewhere", target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            pytest.skip(f"symlinks unavailable on this platform: {error}")

        found = await listing(LocalConnector(root))
        assert found == ["allen/real"]

    def test_resolve_refuses_a_symlink_escape(self, tmp_path: Path) -> None:
        secret = tmp_path / "outside.txt"
        secret.write_bytes(b"secret")
        root = tmp_path / "maildir"
        root.mkdir()
        try:
            os.symlink(secret, root / "link")
        except (OSError, NotImplementedError) as error:
            pytest.skip(f"symlinks unavailable on this platform: {error}")

        with pytest.raises(PathEscape):
            LocalConnector(root).resolve("link")


class TestFetch:
    async def test_it_returns_a_document(self, corpus: Path) -> None:
        document = await LocalConnector(corpus).fetch("allen/sent/1")
        assert document.external_id == "allen/sent/1"
        assert document.uri == "allen/sent/1"
        assert document.source_system is SourceSystem.LOCAL
        assert "Thursday" in document.body
        assert document.content_hash

    async def test_acls_come_from_the_participants(self, corpus: Path) -> None:
        acls = await LocalConnector(corpus).acls("taylor/inbox/1")
        assert {entry.principal_id for entry in acls} == {
            "local:jane.taylor@example.com",
            "local:phillip.allen@example.com",
            "local:ops@example.com",
        }

    async def test_fetching_a_non_mail_file_raises(self, corpus: Path) -> None:
        (corpus / "allen" / "README").write_text("not mail at all", encoding="utf-8")
        with pytest.raises(UnparsableMessage):
            await LocalConnector(corpus).fetch("allen/README")

    async def test_fetching_outside_the_corpus_raises_path_escape(self, corpus: Path) -> None:
        with pytest.raises(PathEscape):
            await LocalConnector(corpus).fetch("../outside")

    async def test_an_oversized_file_is_refused(self, tmp_path: Path) -> None:
        """A corrupted download must not be read into memory during a corpus walk (§30)."""
        from jutsu_connectors.local import MAX_MESSAGE_BYTES

        root = write_corpus(
            tmp_path / "maildir",
            [Message(path="allen/huge", raw=b"From: a@example.com\r\n\r\n" + b"x" * 64)],
        )
        connector = LocalConnector(root)
        (root / "allen" / "huge").write_bytes(b"x" * (MAX_MESSAGE_BYTES + 1))
        with pytest.raises(UnparsableMessage, match="maximum size"):
            await connector.fetch("allen/huge")

    async def test_the_thread_id_is_a_local_best_effort(self, corpus: Path) -> None:
        """A single message knows its parent, not its thread. `sample_enron` knows better,
        and the two disagree by design."""
        document = await LocalConnector(corpus).fetch("taylor/inbox/1")
        assert document.thread_id == "a1@example.com"
