"""Filenames Windows cannot address without help (§19, the Enron corpus).

**Every Enron message filename ends in a dot** — `1.`, `10.`, `1387.` — and the Win32
path parser strips a trailing dot before the filesystem sees the name. The failure is not
an exception anywhere useful: `os.walk` lists `1.` correctly, `Path.exists()` then answers
False, and the read raises `FileNotFoundError`, which `_scan` counts as "unparsable".

Measured on the real corpus before the fix: **517 401 files discovered, 517 401
unparsable, 0 sampled**, and the command exited 0. That is the worst available shape — a
total data-loading failure reported as success.

These tests are written to fail on the pre-fix code. On POSIX a trailing dot is an
ordinary filename character, so they exercise the same paths and simply pass for a
different reason, which is what makes them worth running everywhere rather than skipping.

The corpus fixtures are built here with explicit CRLF for the reason `.gitattributes`
forces: `* text=auto eol=lf` would rewrite a committed mail file into something that
parses differently from the mail it imitates.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from jutsu_connectors import local as local_module
from jutsu_connectors.local import LocalConnector, PathEscape, os_path, real_path

CRLF = "\r\n"

#: A real message shape: the headers `acls_for` derives grants from, and a body.
MESSAGE = (
    CRLF.join(
        [
            "Message-ID: <16159836.1075855377439.JavaMail.evans@thyme>",
            "Date: Mon, 3 Mar 2003 09:00:00 -0800",
            "From: allen-p@enron.example",
            "To: taylor-m@enron.example",
            "Subject: trailing dot",
        ]
    )
    + CRLF
    + CRLF
    + "the body of a message whose filename ends in a dot"
).encode("utf-8")

PLAIN = b"Message-ID: <plain@x>\r\nFrom: a@example.com\r\nTo: b@example.com\r\nSubject: plain\r\n\r\nbody"


def _write(path: Path, payload: bytes) -> None:
    """Write through the extended form, since the ordinary one cannot create `1.`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(os_path(path), "wb") as handle:
        handle.write(payload)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """One trailing-dot message and one ordinary one, in the Enron layout."""
    root = tmp_path / "maildir"
    _write(root / "allen-p" / "inbox" / "1.", MESSAGE)
    _write(root / "allen-p" / "inbox" / "plain", PLAIN)
    return root


async def listing(connector: LocalConnector) -> list[str]:
    return [external_id async for external_id in connector.list_since(None)]


class TestTheFixtureItself:
    """If the fixture cannot create the file, every test below proves nothing."""

    def test_a_trailing_dot_file_was_actually_created(self, corpus: Path) -> None:
        names = sorted(p.name for p in (corpus / "allen-p" / "inbox").iterdir())
        assert "1." in names, "the fixture did not create a trailing-dot filename"

    def test_the_ordinary_path_cannot_see_it_on_windows(self, corpus: Path) -> None:
        """The defect, stated. On POSIX the ordinary path works and this is vacuous."""
        dotted = corpus / "allen-p" / "inbox" / "1."
        if os.name == "nt":
            assert not dotted.exists(), "Windows resolved a trailing dot — defect gone?"
        assert os.path.exists(os_path(dotted)), "the extended form must always see it"


class TestDiscovery:
    async def test_a_trailing_dot_file_is_listed(self, corpus: Path) -> None:
        assert "allen-p/inbox/1." in await listing(LocalConnector(corpus))

    async def test_ordinary_files_are_still_listed(self, corpus: Path) -> None:
        assert "allen-p/inbox/plain" in await listing(LocalConnector(corpus))

    async def test_the_identifier_keeps_the_dot(self, corpus: Path) -> None:
        """The dot is part of the document's identity.

        `external_id` is the §4.14 idempotency key. Trimming the dot would make the
        identifier differ between Windows and Linux for the same message, so a corpus
        re-ingested on the other platform would look entirely new.
        """
        listed = await listing(LocalConnector(corpus))
        assert not any(name.endswith("/1") for name in listed)


class TestReading:
    async def test_the_contents_are_actually_readable(self, corpus: Path) -> None:
        """The heart of it: discovery already worked before the fix; reading did not."""
        document = await LocalConnector(corpus).fetch("allen-p/inbox/1.")
        assert "the body of a message whose filename ends in a dot" in document.body

    async def test_it_is_not_silently_turned_into_an_unparsable_document(
        self, corpus: Path
    ) -> None:
        """The exact pre-fix symptom, as a test.

        Before the fix this raised `FileNotFoundError`, `_scan` caught it as `OSError`
        and counted the message unparsable — so a readable message became corpus
        wastage with no error anywhere.
        """
        connector = LocalConnector(corpus)
        for external_id in await listing(connector):
            document = await connector.fetch(external_id)
            assert document.body, f"{external_id} produced an empty document"

    async def test_the_headers_survive(self, corpus: Path) -> None:
        document = await LocalConnector(corpus).fetch("allen-p/inbox/1.")
        assert document.title == "trailing dot"
        assert document.external_id == "allen-p/inbox/1."

    async def test_acls_are_derived_from_a_trailing_dot_message(self, corpus: Path) -> None:
        """ACL capture reads the file too, so it broke in the same way."""
        grants = await LocalConnector(corpus).acls("allen-p/inbox/1.")
        assert grants, "no grants derived from a readable message"

    async def test_an_ordinary_file_still_reads(self, corpus: Path) -> None:
        assert (await LocalConnector(corpus).fetch("allen-p/inbox/plain")).title == "plain"


class TestTheCursorPath:
    async def test_the_cursor_can_stat_a_trailing_dot_file(self, corpus: Path) -> None:
        """`list_since` stats each file when a cursor is set — a second broken call site.

        With a cursor, an unstattable file was skipped by the `except OSError: continue`,
        so an incremental run would have silently listed nothing at all.
        """
        past = "2000-01-01T00:00:00+00:00"
        assert "allen-p/inbox/1." in await listing_since(LocalConnector(corpus), past)

    async def test_a_future_cursor_still_filters(self, corpus: Path) -> None:
        """The filter must still work, not just not-crash."""
        future = "2099-01-01T00:00:00+00:00"
        assert await listing_since(LocalConnector(corpus), future) == []


async def listing_since(connector: LocalConnector, cursor: str) -> list[str]:
    return [external_id async for external_id in connector.list_since(cursor)]


class TestContainmentIsUnchanged:
    """The fix must not buy readability with a hole in the security control."""

    @pytest.mark.parametrize(
        "identifier", ["../outside.", "/etc/passwd", "allen-p/../../escape.", "a\\b."]
    )
    def test_traversal_is_still_refused(self, corpus: Path, identifier: str) -> None:
        with pytest.raises(PathEscape):
            LocalConnector(corpus).resolve(identifier)

    def test_a_legitimate_trailing_dot_identifier_resolves_inside(self, corpus: Path) -> None:
        target = LocalConnector(corpus).resolve("allen-p/inbox/1.")
        assert target.is_relative_to(corpus.resolve())
        assert target.name == "1."

    def test_real_path_returns_an_ordinary_form(self, corpus: Path) -> None:
        """The extended prefix is an instruction to the parser, not part of identity.

        Letting `\\\\?\\` into a resolved path would put it into `external_id` on
        Windows and nowhere else, changing every idempotency key on one platform.
        """
        resolved = real_path(corpus / "allen-p" / "inbox" / "1.")
        assert not str(resolved).startswith("\\\\?\\")
        assert str(resolved).endswith("1.")


class TestOsPathHelper:
    """Both branches, on every platform.

    The platform flag is monkeypatched rather than skipped around. A skip here would be
    a permanent hole — CI is Linux and the developer machine is Windows, so each would
    forever leave the other branch unexecuted, and the coverage clause treats an
    unclassified skip as a reason not to report a figure at all.
    """

    @pytest.fixture
    def as_posix_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(local_module, "_WINDOWS", False)

    @pytest.fixture
    def as_windows_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(local_module, "_WINDOWS", True)

    def test_it_is_a_no_op_off_windows(self, tmp_path: Path, as_posix_platform: None) -> None:
        assert os_path(tmp_path / "1.") == str(tmp_path / "1.")

    def test_it_prefixes_on_windows(self, tmp_path: Path, as_windows_platform: None) -> None:
        assert os_path(tmp_path / "1.").startswith("\\\\?\\")

    def test_it_is_idempotent(self, tmp_path: Path, as_windows_platform: None) -> None:
        """Prefixing twice would produce a path naming a directory called `?`."""
        once = os_path(tmp_path / "1.")
        assert os_path(Path(once)) == once

    def test_a_unc_share_gets_the_unc_form(self, as_windows_platform: None) -> None:
        r"""`\\server\share` must become `\\?\UNC\server\share`, not `\\?\\\server\share`."""
        assert os_path(Path("\\\\server\\share\\1.")) == "\\\\?\\UNC\\server\\share\\1."

    def test_real_path_is_plain_resolve_off_windows(
        self, corpus: Path, as_posix_platform: None
    ) -> None:
        """Requirement: POSIX behaviour is untouched."""
        target = corpus / "allen-p" / "inbox" / "plain"
        assert real_path(target) == target.resolve()
