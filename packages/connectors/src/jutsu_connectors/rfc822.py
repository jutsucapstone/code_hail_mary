"""RFC 822 / RFC 5322 mail into `RawDocument` (spec §9, §19).

**Everything here treats its input as hostile.** A mail corpus is third-party text that
nobody curated: headers are truncated mid-word, charsets are declared wrongly or not at
all, `Date` is in a format no RFC ever described, MIME boundaries do not close, and some
files are not mail at all. §30 says to treat retrieved content as untrusted, and the
practical form of that here is that **no malformed message may take down an ingestion
run** — a parse either yields a document or raises `UnparsableMessage`, and never
propagates a `UnicodeDecodeError` from four frames down.

**Nothing in this module logs.** Not a subject, not an address, not a body, not a
fragment of one in an exception message. §4.9 forbids PII in logs and this is the module
that has the most of it to hand; the defects a message carries are recorded on the parsed
object, where a caller can count them, rather than written anywhere.

Two things are deliberately *not* done here:

  * **Thread identity.** A single message knows its parent, not its thread. Assigning a
    canonical thread id needs the whole corpus, which is `threads.py`. `parse_message`
    extracts `message_id`, `in_reply_to` and `references` and stops there.
  * **Masking.** `RawDocument.body` is the ORIGINAL text, because chunk offsets and
    citations are measured against it (§9.2). Masking is S4 and happens downstream.
"""

from __future__ import annotations

import email.policy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Final

from jutsu_core import AclEntry, RawDocument, SourceSystem
from jutsu_core.domains import DomainError, normalise_email

__all__ = [
    "ParsedMessage",
    "UnparsableMessage",
    "acls_for",
    "normalise_message_id",
    "parse_message",
    "to_raw_document",
    "utc_from_timestamp",
]


class UnparsableMessage(ValueError):
    """The bytes are not a mail message.

    Raised rather than returning a degenerate document, because a corpus directory
    contains README files, `.DS_Store`, and the occasional truncated download. Those
    should be skipped and counted, not ingested as documents with an empty body.
    """


#: Headers whose presence means "this is probably mail". A file with none of them is
#: something else that happened to be in the directory.
_MAIL_HEADERS: Final = ("message-id", "from", "date", "subject", "to")

#: What a text part has to be for the body to be taken from it.
_PREFERRED_BODY_TYPE: Final = "text/plain"

#: Recipient headers that become ACL principals. `Bcc` is included because the Enron
#: corpus preserves it in the sender's own copy, and a blind recipient did receive the
#: message — excluding them would produce a grant set that is wrong in the direction that
#: hides evidence from somebody who legitimately saw it.
_RECIPIENT_HEADERS: Final = ("to", "cc", "bcc")


def normalise_message_id(value: str | None) -> str | None:
    """Strip the angle brackets and surrounding whitespace from a Message-ID.

    Case is left alone. RFC 5322 makes the left-hand side of a message id
    case-sensitive, and while almost every mail system in practice treats ids
    case-insensitively, folding case here would merge two distinct ids and silently join
    two threads. Threading errors are hard to see and impossible to undo once the graph
    is built.
    """
    if value is None:
        return None
    cleaned = value.strip().strip("<>").strip()
    return cleaned or None


def _header(message: EmailMessage, name: str) -> str | None:
    """One header as text, or None.

    Wrapped because `policy.default` decodes RFC 2047 encoded words on access, and a
    truncated or mis-declared encoded word raises there rather than at parse time. A
    header that cannot be decoded is treated as absent — the alternative is a crash in
    the middle of a corpus walk over a message nobody can identify afterwards.
    """
    try:
        value = message.get(name)
    except Exception:
        return None
    if value is None:
        return None
    try:
        text = str(value)
    except Exception:
        return None
    return text.strip() or None


def _addresses(message: EmailMessage, names: tuple[str, ...]) -> tuple[str, ...]:
    """Normalised addresses from the named headers, in first-seen order.

    `getaddresses` is tolerant by design and returns `('', '')` pairs for junk, which are
    dropped. Anything `normalise_email` refuses is dropped too: this feeds ACL principal
    ids, and a malformed principal is worse than a missing one — it grants access to a
    string nobody will ever match.
    """
    raw: list[str] = []
    for name in names:
        value = _header(message, name)
        if value:
            raw.append(value)

    seen: dict[str, None] = {}
    for _display, address in getaddresses(raw):
        if not address:
            continue
        try:
            seen.setdefault(normalise_email(address), None)
        except DomainError:
            continue
    return tuple(seen)


def _references(message: EmailMessage) -> tuple[str, ...]:
    """Every id in `References`, oldest first, with `In-Reply-To` appended if new.

    `References` is a space-separated list that mail clients truncate from the middle
    when it grows, so it is a hint rather than a chain. `threads.py` unions every id it
    finds, which makes a truncated list harmless — the component still joins through
    whatever ids survive.
    """
    ids: dict[str, None] = {}
    for header in ("references", "in-reply-to"):
        value = _header(message, header)
        if not value:
            continue
        for token in value.replace(",", " ").split():
            identifier = normalise_message_id(token)
            if identifier:
                ids.setdefault(identifier, None)
    return tuple(ids)


def _body(message: EmailMessage) -> tuple[str, str, list[str]]:
    """The best text part, its media type, and the defects found on the way.

    `text/plain` wins; the first other `text/*` part is the fallback. HTML is returned as
    it stands rather than stripped — stripping is a normalisation decision with its own
    correctness questions, and doing it here would silently change the offsets every
    citation is measured against.

    Attachments are skipped entirely: this pipeline indexes text, and §8 puts raw source
    blobs in object storage rather than in the database.

    Defects are **counted, not logged**. A part that will not decode is a fact worth
    knowing across a corpus — "3% of this sample has an undecodable body" is the kind of
    thing that should be measurable — but the part itself is message content and §4.9
    keeps it out of logs. Returning the names lets a caller aggregate them.
    """
    best: tuple[str, str] | None = None
    defects: list[str] = []

    for part in message.walk():
        if part.get_content_maintype() != "text":
            continue
        if part.get_content_disposition() == "attachment":
            continue

        content_type = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
        except Exception as error:
            defects.append(f"UndecodablePart:{type(error).__name__}")
            continue
        if not isinstance(payload, bytes):
            defects.append("NonBytesPayload")
            continue

        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            # A charset name the codec registry has never heard of. Common in this
            # corpus, and not a reason to lose the message.
            defects.append("UnknownCharset")
            text = payload.decode("utf-8", errors="replace")

        if content_type == _PREFERRED_BODY_TYPE:
            return text, content_type, defects
        if best is None:
            best = (text, content_type)

    if best is None:
        defects.append("NoTextPart")
        return "", _PREFERRED_BODY_TYPE, defects
    return best[0], best[1], defects


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """One mail message, reduced to what the pipeline needs.

    Separate from `RawDocument` on purpose. A `RawDocument` carries a `thread_id`, and a
    message cannot know its own thread — only its parent. Keeping the two apart is what
    lets `threads.py` assign thread identity over a whole corpus without this module
    having to guess at it.
    """

    message_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    subject: str
    body: str
    body_mime: str
    sender: str | None
    recipients: tuple[str, ...]
    sent_at: datetime | None
    #: Names of the problems found, never the values that caused them. Counting these
    #: across a corpus is how you learn it is 3% junk without logging any of it.
    defects: tuple[str, ...] = field(default_factory=tuple)

    @property
    def participants(self) -> tuple[str, ...]:
        """Sender and recipients, deduplicated, sender first."""
        seen: dict[str, None] = {}
        if self.sender:
            seen.setdefault(self.sender, None)
        for address in self.recipients:
            seen.setdefault(address, None)
        return tuple(seen)


def parse_message(data: bytes) -> ParsedMessage:
    """Bytes to a `ParsedMessage`, or `UnparsableMessage`.

    Parsed from bytes rather than text deliberately: the charset is declared *inside* the
    message, so decoding before parsing means guessing it first and getting it wrong for
    exactly the non-ASCII messages that matter.
    """
    try:
        message = BytesParser(policy=email.policy.default).parsebytes(data)
    except Exception as error:
        raise UnparsableMessage("the bytes could not be parsed as a mail message") from error

    if not isinstance(message, EmailMessage):  # pragma: no cover - policy.default guarantees it
        raise UnparsableMessage("the parser did not produce a mail message")

    present = [name for name in _MAIL_HEADERS if _header(message, name) is not None]
    if not present:
        raise UnparsableMessage("no recognisable mail headers")

    defects: list[str] = [type(defect).__name__ for defect in message.defects]

    sent_at: datetime | None = None
    date_header = _header(message, "date")
    if date_header:
        try:
            parsed_date = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            defects.append("UnparsableDate")
        else:
            # A `Date` with no zone is not UTC, it is unknown. Treating it as UTC is a
            # guess that shifts a message by up to twelve hours, and ordering within a
            # thread is exactly what a decision ledger reads.
            sent_at = parsed_date if parsed_date.tzinfo is not None else None
            if sent_at is None:
                defects.append("NaiveDate")

    sender_addresses = _addresses(message, ("from",))
    body, body_mime, body_defects = _body(message)
    defects.extend(body_defects)

    return ParsedMessage(
        message_id=normalise_message_id(_header(message, "message-id")),
        in_reply_to=normalise_message_id(_header(message, "in-reply-to")),
        references=_references(message),
        subject=_header(message, "subject") or "",
        body=body,
        body_mime=body_mime,
        sender=sender_addresses[0] if sender_addresses else None,
        recipients=_addresses(message, _RECIPIENT_HEADERS),
        sent_at=sent_at,
        defects=tuple(defects),
    )


def acls_for(
    parsed: ParsedMessage, *, source_system: SourceSystem = SourceSystem.LOCAL
) -> list[AclEntry]:
    """Read grants derived from the message's own participants (ADR 0008, ADR 0010).

    Everyone on the message may read it, and nobody else. That is not an invented policy:
    it is the access the mail system itself granted, recovered from the headers, and it
    is the only defensible reading of a corpus that ships no ACLs of its own.

    **Principals are namespaced `{source_system}:{subject}`** — for this corpus,
    `local:someone@example.com`. The namespace is not decoration: a bare subject means
    nothing outside the system that issued it, and without one a Slack member id could in
    principle match a GitHub grant. `source_identities` maps a signed-in user to the
    subjects they own, in exactly this form.

    **The subject here really is an email address, and that is not a compromise.** A
    public mail corpus has no identity provider; the address *is* the identity the source
    issues. What ADR 0010 changed is that it is now labelled a `local:` subject rather
    than left looking like a portable one — a real connector emits `slack:U0...` or
    `m365:{oid}`, and the two can never collide.

    Sorted, so `acl_hash_of` is stable regardless of header order.
    """
    principals = sorted(set(parsed.participants))
    return [
        AclEntry(principal_type="user", principal_id=f"{source_system.value}:{principal}")
        for principal in principals
    ]


def to_raw_document(
    parsed: ParsedMessage,
    *,
    external_id: str,
    source_system: SourceSystem,
    uri: str | None,
    thread_id: str | None,
    fallback_sent_at: datetime,
) -> RawDocument:
    """A `ParsedMessage` plus its corpus context, as the pipeline's input document.

    `fallback_sent_at` covers a message whose `Date` is missing, unparseable or
    zone-less. `RawDocument.created_at` is not optional and every downstream ordering
    reads it, so the caller supplies something real — the file's modification time — and
    the substitution is recorded in `raw_metadata` rather than hidden.
    """
    return RawDocument(
        external_id=external_id,
        source_system=source_system,
        uri=uri,
        title=parsed.subject,
        body=parsed.body,
        mime=parsed.body_mime,
        author_external_id=parsed.sender,
        participant_external_ids=list(parsed.participants),
        thread_id=thread_id,
        created_at=parsed.sent_at or fallback_sent_at,
        modified_at=None,
        acls=acls_for(parsed),
        raw_metadata={
            "message_id": parsed.message_id,
            "in_reply_to": parsed.in_reply_to,
            "references": list(parsed.references),
            "defects": list(parsed.defects),
            "date_from_header": parsed.sent_at is not None,
        },
    )


def utc_from_timestamp(value: float) -> datetime:
    """A filesystem mtime as an aware UTC datetime.

    Aware, always. S2 established that a naive instant in this system is a defect, and a
    fallback timestamp is exactly the kind of value that acquires a wrong zone quietly.
    """
    return datetime.fromtimestamp(value, tz=UTC)
