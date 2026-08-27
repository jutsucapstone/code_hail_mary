"""RFC 822 parsing, and everything a real corpus does to a parser (§9, §19, §30).

The rule under test throughout: **a malformed message yields a document or an
`UnparsableMessage`, and never anything else.** A corpus walk that dies on message
40,000 of 50,000 has cost more than the message.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from jutsu_connectors.rfc822 import (
    UnparsableMessage,
    acls_for,
    normalise_message_id,
    parse_message,
    to_raw_document,
    utc_from_timestamp,
)
from jutsu_core import SourceSystem, acl_hash_of, content_hash_of

CRLF = "\r\n"
FALLBACK = datetime(2026, 1, 1, tzinfo=UTC)


def build(headers: dict[str, str], body: str = "", encoding: str = "utf-8") -> bytes:
    lines = [f"{name}: {value}" for name, value in headers.items()]
    return (CRLF.join(lines) + CRLF + CRLF + body).encode(encoding)


SIMPLE = build(
    {
        "Message-ID": "<a1@example.com>",
        "Date": "Mon, 12 Jan 2026 09:15:00 -0600",
        "From": "Phillip Allen <phillip.allen@example.com>",
        "To": "jane.taylor@example.com, ops@example.com",
        "Subject": "Falcon rollout",
    },
    "We should hold the production run until Thursday.",
)


class TestHappyPath:
    def test_headers_and_body(self) -> None:
        parsed = parse_message(SIMPLE)
        assert parsed.message_id == "a1@example.com"
        assert parsed.subject == "Falcon rollout"
        assert parsed.sender == "phillip.allen@example.com"
        assert parsed.recipients == ("jane.taylor@example.com", "ops@example.com")
        assert "Thursday" in parsed.body
        assert parsed.body_mime == "text/plain"

    def test_the_date_keeps_its_offset(self) -> None:
        """A `Date` is not UTC; ordering inside a thread reads this."""
        parsed = parse_message(SIMPLE)
        assert parsed.sent_at is not None
        assert parsed.sent_at.utcoffset() is not None
        assert parsed.sent_at == datetime(2026, 1, 12, 15, 15, tzinfo=UTC)

    def test_participants_put_the_sender_first(self) -> None:
        parsed = parse_message(SIMPLE)
        assert parsed.participants[0] == "phillip.allen@example.com"
        assert set(parsed.participants) == {
            "phillip.allen@example.com",
            "jane.taylor@example.com",
            "ops@example.com",
        }

    def test_addresses_are_normalised(self) -> None:
        """One rule for addresses across the product — `jutsu_core.domains`."""
        parsed = parse_message(
            build({"From": "Phillip <Phillip.Allen@EXAMPLE.COM>", "Subject": "x"}, "body")
        )
        assert parsed.sender == "Phillip.Allen@example.com"


class TestMessageIdNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("<a1@example.com>", "a1@example.com"),
            ("  <a1@example.com>  ", "a1@example.com"),
            ("a1@example.com", "a1@example.com"),
            ("<>", None),
            ("", None),
            (None, None),
        ],
    )
    def test_brackets_and_whitespace_are_stripped(
        self, raw: str | None, expected: str | None
    ) -> None:
        assert normalise_message_id(raw) == expected

    def test_case_is_preserved(self) -> None:
        """Folding case would merge two distinct ids and silently join two threads."""
        assert normalise_message_id("<A1@Example.com>") == "A1@Example.com"


class TestEncodedHeaders:
    def test_rfc2047_subject_is_decoded(self) -> None:
        encoded = "=?utf-8?B?" + "8J+ZjyDgpKjgpK7gpLjgpY3gpKTgpYc=" + "?="
        parsed = parse_message(build({"Subject": encoded, "From": "a@example.com"}, "body"))
        assert parsed.subject == "🙏 नमस्ते"

    def test_a_broken_encoded_word_does_not_raise(self) -> None:
        """Truncated encoded words are common; a crash mid-corpus is not acceptable."""
        parsed = parse_message(
            build({"Subject": "=?utf-8?B?bm90LXZhbGlk", "From": "a@example.com"}, "body")
        )
        assert isinstance(parsed.subject, str)

    def test_a_non_ascii_body_survives(self) -> None:
        parsed = parse_message(
            build(
                {
                    "From": "a@example.com",
                    "Subject": "x",
                    "Content-Type": 'text/plain; charset="utf-8"',
                },
                "नमस्ते 🙏 done",
            )
        )
        assert "नमस्ते 🙏" in parsed.body


class TestMalformedInput:
    def test_a_file_that_is_not_mail_is_refused(self) -> None:
        with pytest.raises(UnparsableMessage, match="no recognisable mail headers"):
            parse_message(b"just some text in a corpus directory\n")

    def test_empty_bytes_are_refused(self) -> None:
        with pytest.raises(UnparsableMessage):
            parse_message(b"")

    def test_binary_rubbish_is_refused_not_crashed_on(self) -> None:
        with pytest.raises(UnparsableMessage):
            parse_message(bytes(range(256)))

    def test_a_missing_message_id_is_not_fatal(self) -> None:
        parsed = parse_message(build({"From": "a@example.com", "Subject": "x"}, "body"))
        assert parsed.message_id is None
        assert parsed.body == "body"

    def test_an_unparseable_date_is_recorded_not_raised(self) -> None:
        parsed = parse_message(
            build({"From": "a@example.com", "Date": "last Tuesday-ish", "Subject": "x"}, "b")
        )
        assert parsed.sent_at is None
        assert "UnparsableDate" in parsed.defects

    def test_a_zoneless_date_is_refused_rather_than_assumed_utc(self) -> None:
        """Assuming UTC shifts a message by up to twelve hours, and thread order reads it."""
        parsed = parse_message(
            build(
                {"From": "a@example.com", "Date": "Mon, 12 Jan 2026 09:15:00", "Subject": "x"}, "b"
            )
        )
        assert parsed.sent_at is None
        assert "NaiveDate" in parsed.defects

    def test_an_unclosed_mime_boundary_is_tolerated(self) -> None:
        raw = (
            b"From: a@example.com\r\nSubject: x\r\n"
            b'Content-Type: multipart/mixed; boundary="BOUND"\r\n\r\n'
            b"--BOUND\r\nContent-Type: text/plain\r\n\r\nthe text\r\n"
        )
        parsed = parse_message(raw)
        assert "the text" in parsed.body

    def test_an_unknown_charset_falls_back_and_is_recorded(self) -> None:
        raw = (
            b"From: a@example.com\r\nSubject: x\r\n"
            b'Content-Type: text/plain; charset="x-not-a-charset"\r\n\r\nplain text\r\n'
        )
        parsed = parse_message(raw)
        assert "plain text" in parsed.body
        assert "UnknownCharset" in parsed.defects

    def test_a_message_with_no_text_part_yields_an_empty_body(self) -> None:
        raw = b"From: a@example.com\r\nSubject: x\r\nContent-Type: image/png\r\n\r\n\x89PNG\r\n"
        parsed = parse_message(raw)
        assert parsed.body == ""
        assert "NoTextPart" in parsed.defects

    def test_a_malformed_address_is_dropped_not_stored(self) -> None:
        """A malformed principal grants access to a string nobody will ever match."""
        parsed = parse_message(
            build({"From": "a@example.com", "To": "not-an-address, b@example.com"}, "x")
        )
        assert parsed.recipients == ("b@example.com",)

    def test_defects_never_contain_message_content(self) -> None:
        """§4.9 — the defect names are countable; the values that caused them are not."""
        parsed = parse_message(
            build({"From": "a@example.com", "Date": "nonsense", "Subject": "Secret Project"}, "b")
        )
        for defect in parsed.defects:
            assert "Secret" not in defect
            assert "nonsense" not in defect


class TestBodySelection:
    def test_plain_text_wins_over_html(self) -> None:
        raw = (
            b"From: a@example.com\r\nSubject: x\r\n"
            b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
            b"--B\r\nContent-Type: text/html\r\n\r\n<p>html version</p>\r\n"
            b"--B\r\nContent-Type: text/plain\r\n\r\nplain version\r\n"
            b"--B--\r\n"
        )
        parsed = parse_message(raw)
        assert "plain version" in parsed.body
        assert parsed.body_mime == "text/plain"

    def test_html_is_used_when_there_is_no_plain_part(self) -> None:
        """Returned unstripped — stripping would move every offset a citation indexes."""
        raw = (
            b"From: a@example.com\r\nSubject: x\r\n"
            b"Content-Type: text/html\r\n\r\n<p>only html</p>\r\n"
        )
        parsed = parse_message(raw)
        assert "<p>only html</p>" in parsed.body
        assert parsed.body_mime == "text/html"

    def test_attachments_are_not_used_as_the_body(self) -> None:
        raw = (
            b"From: a@example.com\r\nSubject: x\r\n"
            b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
            b"--B\r\nContent-Type: text/plain\r\n"
            b'Content-Disposition: attachment; filename="notes.txt"\r\n\r\nattached\r\n'
            b"--B\r\nContent-Type: text/plain\r\n\r\nthe real body\r\n"
            b"--B--\r\n"
        )
        parsed = parse_message(raw)
        assert "the real body" in parsed.body
        assert "attached" not in parsed.body


class TestAclDerivation:
    def test_every_participant_gets_a_read_grant(self) -> None:
        """ADR 0008 — the access the mail system granted, recovered from the headers."""
        acls = acls_for(parse_message(SIMPLE))
        assert {entry.principal_id for entry in acls} == {
            "local:phillip.allen@example.com",
            "local:jane.taylor@example.com",
            "local:ops@example.com",
        }
        assert all(entry.principal_type == "user" for entry in acls)
        assert all(entry.permission == "read" for entry in acls)

    def test_grants_are_sorted_so_the_hash_is_stable(self) -> None:
        """`acl_hash_of` detects permission drift; header order is not drift."""
        reordered = build(
            {
                "Message-ID": "<a1@example.com>",
                "Date": "Mon, 12 Jan 2026 09:15:00 -0600",
                "From": "Phillip Allen <phillip.allen@example.com>",
                "To": "ops@example.com, jane.taylor@example.com",
                "Subject": "Falcon rollout",
            },
            "We should hold the production run until Thursday.",
        )
        assert acl_hash_of(acls_for(parse_message(SIMPLE))) == acl_hash_of(
            acls_for(parse_message(reordered))
        )

    def test_a_message_with_no_recipients_still_grants_the_sender(self) -> None:
        acls = acls_for(parse_message(build({"From": "a@example.com", "Subject": "x"}, "b")))
        assert [entry.principal_id for entry in acls] == ["local:a@example.com"]

    def test_principals_are_namespaced_by_source_system(self) -> None:
        """ADR 0010. A bare subject means nothing outside the system that issued it.

        Without the namespace a Slack member id and a GitHub numeric id live in the same
        string space, and a grant from one could in principle match a principal from the
        other. The prefix makes that impossible rather than improbable.
        """
        for system in (SourceSystem.LOCAL, SourceSystem.SLACK, SourceSystem.GITHUB):
            acls = acls_for(parse_message(SIMPLE), source_system=system)
            assert all(entry.principal_id.startswith(f"{system.value}:") for entry in acls), system

    def test_the_same_subject_in_two_systems_yields_different_principals(self) -> None:
        """The collision this design exists to prevent, asserted directly."""
        local = {
            e.principal_id
            for e in acls_for(parse_message(SIMPLE), source_system=SourceSystem.LOCAL)
        }
        slack = {
            e.principal_id
            for e in acls_for(parse_message(SIMPLE), source_system=SourceSystem.SLACK)
        }
        assert local.isdisjoint(slack)

    def test_a_message_with_no_participants_has_no_grants(self) -> None:
        """Nobody is a real state, not an error (§17 test 1). It means nobody sees it."""
        acls = acls_for(parse_message(build({"Subject": "x", "Date": "bad"}, "b")))
        assert acls == []


class TestRawDocument:
    def _document(self, data: bytes = SIMPLE) -> object:
        return to_raw_document(
            parse_message(data),
            external_id="allen/sent/1",
            source_system=SourceSystem.LOCAL,
            uri="allen/sent/1",
            thread_id="a1@example.com",
            fallback_sent_at=FALLBACK,
        )

    def test_content_hash_is_deterministic(self) -> None:
        first = to_raw_document(
            parse_message(SIMPLE),
            external_id="x",
            source_system=SourceSystem.LOCAL,
            uri="x",
            thread_id=None,
            fallback_sent_at=FALLBACK,
        )
        second = to_raw_document(
            parse_message(SIMPLE),
            external_id="x",
            source_system=SourceSystem.LOCAL,
            uri="x",
            thread_id=None,
            fallback_sent_at=FALLBACK,
        )
        assert first.content_hash == second.content_hash
        assert first.content_hash == content_hash_of(first.body)

    def test_content_hash_ignores_the_identifier_and_thread(self) -> None:
        """§4.14 keys on content. Re-filing a message is not new content."""
        parsed = parse_message(SIMPLE)
        one = to_raw_document(
            parsed,
            external_id="allen/sent/1",
            source_system=SourceSystem.LOCAL,
            uri="allen/sent/1",
            thread_id="a1@example.com",
            fallback_sent_at=FALLBACK,
        )
        two = to_raw_document(
            parsed,
            external_id="taylor/inbox/7",
            source_system=SourceSystem.LOCAL,
            uri="taylor/inbox/7",
            thread_id="other",
            fallback_sent_at=FALLBACK,
        )
        assert one.content_hash == two.content_hash

    def test_the_body_is_the_original_not_a_masked_copy(self) -> None:
        """S4 masks downstream. Offsets are measured against this text (§9.2)."""
        parsed = parse_message(SIMPLE)
        document = to_raw_document(
            parsed,
            external_id="x",
            source_system=SourceSystem.LOCAL,
            uri="x",
            thread_id=None,
            fallback_sent_at=FALLBACK,
        )
        assert document.body == parsed.body
        assert "[EMAIL_" not in document.body

    def test_a_missing_date_falls_back_and_records_that_it_did(self) -> None:
        document = to_raw_document(
            parse_message(build({"From": "a@example.com", "Subject": "x"}, "b")),
            external_id="x",
            source_system=SourceSystem.LOCAL,
            uri="x",
            thread_id=None,
            fallback_sent_at=FALLBACK,
        )
        assert document.created_at == FALLBACK
        assert document.raw_metadata["date_from_header"] is False

    def test_provenance_survives_into_raw_metadata(self) -> None:
        document = to_raw_document(
            parse_message(SIMPLE),
            external_id="x",
            source_system=SourceSystem.LOCAL,
            uri="allen/sent/1",
            thread_id="a1@example.com",
            fallback_sent_at=FALLBACK,
        )
        assert document.raw_metadata["message_id"] == "a1@example.com"
        assert document.uri == "allen/sent/1"
        assert document.source_system is SourceSystem.LOCAL

    def test_created_at_is_always_aware(self) -> None:
        assert utc_from_timestamp(0).tzinfo is not None
