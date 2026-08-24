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

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from typing import Protocol

__all__ = [
    "ConsoleEmailSender",
    "EmailMessage",
    "EmailSender",
    "RecordingEmailSender",
    "SmtpEmailSender",
    "SmtpSettings",
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


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """Everything the transport needs, and nothing it does not.

    `password` is an application password, never an account password. Gmail rejects the
    latter for SMTP outright, and an account password in Secret Manager would be a
    credential to the whole mailbox rather than to sending alone.
    """

    host: str
    port: int
    username: str
    password: str
    sender: str

    @property
    def uses_starttls(self) -> bool:
        """587 is the submission port and upgrades in-band; 465 is TLS from the first byte.

        Getting this backwards does not degrade gracefully — it hangs until the socket
        times out, which reads like a network problem rather than a configuration one.
        """
        return self.port != 465


class SmtpEmailSender:
    """Delivers over SMTP, for any provider that speaks submission — Gmail included.

    **The one-time secrets are rendered into the body here and nowhere else.** They travel
    on `EmailMessage.secrets` precisely so no transport can splice them into a diagnostic
    dump by accident; this is the single place they are allowed to become text, and it is
    the message itself.

    `smtplib` is synchronous and the send happens on a request path, so it runs in a
    worker thread. Calling it inline would block the event loop for the whole SMTP
    conversation — connect, STARTTLS, auth, DATA — which on a slow provider is hundreds of
    milliseconds during which the process serves nobody.
    """

    def __init__(self, settings: SmtpSettings) -> None:
        self._settings = settings

    def _render(self, message: EmailMessage) -> MimeMessage:
        body = message.body
        if message.secrets:
            rendered = "\n".join(f"  {name}: {value}" for name, value in message.secrets.items())
            body = f"{body}\n\n{rendered}\n"

        mime = MimeMessage()
        mime["From"] = self._settings.sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        # An automated one-time code should not generate an out-of-office reply, and it
        # should not be filed as a conversation to reply into.
        mime["Auto-Submitted"] = "auto-generated"
        mime.set_content(body)
        return mime

    def _deliver(self, mime: MimeMessage) -> None:
        context = ssl.create_default_context()
        settings = self._settings

        if settings.uses_starttls:
            with smtplib.SMTP(settings.host, settings.port, timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(settings.username, settings.password)
                smtp.send_message(mime)
        else:
            with smtplib.SMTP_SSL(
                settings.host, settings.port, timeout=20, context=context
            ) as smtp:
                smtp.login(settings.username, settings.password)
                smtp.send_message(mime)

    async def send(self, message: EmailMessage) -> None:
        await asyncio.to_thread(self._deliver, self._render(message))
        # What was sent, never to whom and never what it contained. An address here would
        # put the customer list in the log aggregator (§4.9); the code would put a live
        # credential there.
        logger.info("email_sent")
