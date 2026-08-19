"""Outbound email.

Passwordless authentication puts email on the critical path for every sign-in, including
the on-call engineer's. That makes the transport a first-class dependency rather than a
detail, and it is why this is an interface with a swappable implementation instead of an
SMTP call inlined into the auth route.

**Nothing here logs a code or a link.** The whole point of a one-time secret is that it
exists in exactly two places — the message and the database hash. A log line carrying it
puts it in a third, which is usually the least protected of the three and the one most
likely to be shipped to an aggregator. §4.9 already forbids PII in logs; this is the
stronger case, because the value is a live credential.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "ConsoleEmailSender",
    "EmailMessage",
    "EmailSender",
    "RecordingEmailSender",
]

logger = logging.getLogger("jutsu.email")


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    body: str
    #: Never logged, never rendered anywhere but the body. Carried separately so a
    #: transport cannot accidentally include it in a diagnostic dump of the message.
    secrets: dict[str, str] = field(default_factory=dict, repr=False)


class EmailSender(Protocol):
    """One method, so a real provider, a queue and a test double are interchangeable."""

    async def send(self, message: EmailMessage) -> None: ...


class ConsoleEmailSender:
    """Development transport.

    Writes the message to stdout so a developer can complete a sign-in without an email
    provider configured. This is the one context where printing the code is correct —
    there is no inbox to reach — and it is why the class is named for its output rather
    than pretending to be a mail transport.

    Refuses to run in production: a "sender" that silently discards mail would make every
    sign-in fail with no error anywhere, which is the worst possible failure shape for an
    authentication system.
    """

    def __init__(self, *, environment: str) -> None:
        if environment == "prod":
            raise RuntimeError(
                "ConsoleEmailSender cannot be used in production — it delivers nothing. "
                "Configure a real transport before deploying."
            )
        self._environment = environment

    async def send(self, message: EmailMessage) -> None:
        rendered = "\n".join(f"  {name}: {value}" for name, value in message.secrets.items())
        print(
            f"\n--- email ({self._environment}) ---\n"
            f"to: {message.to}\nsubject: {message.subject}\n\n{message.body}\n"
            f"{rendered}\n--- end ---\n",
            flush=True,
        )
        # The log line records that a message was sent and to nothing else. No address,
        # no code, no link.
        logger.info("email_sent")


class RecordingEmailSender:
    """Test double. Keeps messages in memory so a test can read the code it must submit.

    Exists so the auth tests exercise the real end-to-end path — issue, deliver, verify —
    rather than reaching into the database for a hash they cannot reverse.
    """

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.messages.append(message)

    @property
    def last(self) -> EmailMessage:
        if not self.messages:
            raise AssertionError("no email was sent")
        return self.messages[-1]
