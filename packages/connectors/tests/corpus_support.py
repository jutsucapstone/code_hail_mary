"""Maildir fixtures, built in code rather than checked in as files.

**These are parser fixtures. They are not corpus data.** Nothing here is seeded into
Postgres, and nothing here stands in for the Enron corpus in any measurement — the real
corpus validation is a separate, explicitly approved step. What these exist for is to
exercise threading, containment and determinism, which cannot be tested at all without
*some* mail on disk.

Written from Python rather than committed as `.eml` files, and the reason is concrete:
`.gitattributes` sets `* text=auto eol=lf`, so git would rewrite CRLF line endings in a
committed mail file. Mail is line-ending sensitive — a MIME boundary is defined in terms
of CRLF — so the checked-in fixtures would parse differently from the files they imitate,
and differently again depending on whose checkout they came from. Constructing them here
with explicit `\\r\\n` makes them exact and makes them diffable as source.

**Deliberately not `conftest.py`.** `packages/db/tests/conftest.py` already exists, and a
second `conftest` module under the `packages` tree makes `mypy packages` ambiguous — at
which point it checks nothing at all. The Makefile records this; the graph suite hit it
first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["THREADED_CORPUS", "Message", "write_corpus"]

CRLF = "\r\n"


@dataclass(frozen=True, slots=True)
class Message:
    """One file in the fixture corpus.

    `raw` bypasses header construction entirely, which is how the malformed cases are
    expressed — a truncated MIME boundary or a file that is not mail cannot be built from
    a well-formed header mapping.
    """

    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""
    raw: bytes | None = None
    #: Written as-is when set, so a test can produce a mis-declared charset on purpose.
    encoding: str = "utf-8"

    def render(self) -> bytes:
        if self.raw is not None:
            return self.raw
        lines = [f"{name}: {value}" for name, value in self.headers.items()]
        text = CRLF.join(lines) + CRLF + CRLF + self.body
        return text.encode(self.encoding, errors="strict")


def write_corpus(root: Path, messages: Iterable[Message]) -> Path:
    """Materialise a fixture corpus under `root`. Returns `root`."""
    for message in messages:
        target = root / message.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(message.render())
    return root


def _mail(
    path: str, *, message_id: str, subject: str, sender: str, to: str, body: str, **extra: str
) -> Message:
    headers = {
        "Message-ID": f"<{message_id}>",
        "Date": "Mon, 12 Jan 2026 09:15:00 -0600",
        "From": sender,
        "To": to,
        "Subject": subject,
    }
    headers.update(extra)
    return Message(path=path, headers=headers, body=body)


#: A small corpus with three threads across two custodians, plus one orphan.
#:
#:   thread A  allen/sent/1  ->  allen/sent/2  ->  taylor/inbox/1   (3 messages)
#:   thread B  taylor/inbox/2 -> taylor/inbox/3                     (2 messages)
#:   thread C  allen/inbox/9                                        (1 message, no refs)
#:
#: `taylor/inbox/1` replies to a message in `allen`'s mailbox, which is what makes
#: complete-thread sampling observable: selecting either custodian must pull the other's
#: message in with it.
THREADED_CORPUS: tuple[Message, ...] = (
    _mail(
        "allen/sent/1",
        message_id="a1@example.com",
        subject="Falcon rollout",
        sender="phillip.allen@example.com",
        to="jane.taylor@example.com",
        body="We should hold the production run until Thursday.",
    ),
    _mail(
        "allen/sent/2",
        message_id="a2@example.com",
        subject="RE: Falcon rollout",
        sender="phillip.allen@example.com",
        to="jane.taylor@example.com",
        body="Agreed, Thursday works.",
        **{"In-Reply-To": "<a1@example.com>", "References": "<a1@example.com>"},
    ),
    _mail(
        "taylor/inbox/1",
        message_id="a3@example.com",
        subject="RE: Falcon rollout",
        sender="jane.taylor@example.com",
        to="phillip.allen@example.com",
        body="Confirmed for Thursday.",
        **{
            "In-Reply-To": "<a2@example.com>",
            "References": "<a1@example.com> <a2@example.com>",
            "Cc": "ops@example.com",
        },
    ),
    _mail(
        "taylor/inbox/2",
        message_id="b1@example.com",
        subject="Latency budget",
        sender="jane.taylor@example.com",
        to="ops@example.com",
        body="Retrieval latency sat at two hundred milliseconds.",
    ),
    _mail(
        "taylor/inbox/3",
        message_id="b2@example.com",
        subject="RE: Latency budget",
        sender="ops@example.com",
        to="jane.taylor@example.com",
        body="Inside budget. Cold start is unmeasured.",
        **{"References": "<b1@example.com>"},
    ),
    _mail(
        "allen/inbox/9",
        message_id="c1@example.com",
        subject="Lunch",
        sender="phillip.allen@example.com",
        to="phillip.allen@example.com",
        body="Nothing sensitive here.",
    ),
)
