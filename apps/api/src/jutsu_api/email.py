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

**A one-time secret becomes text exactly once, here.** Templates in `jutsu_api.emails`
emit `[[code]]` and `[[token]]` where a value belongs and never receive the value itself,
so a rendered template can be diffed, snapshotted and asserted against without a live
credential existing in the same object. `fill_secrets` is the single substitution, and it
runs inside the transport at the moment of delivery.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from html import escape
from typing import Protocol

__all__ = [
    "ConsoleEmailSender",
    "EmailMessage",
    "EmailSender",
    "InlineImage",
    "RecordingEmailSender",
    "SmtpEmailSender",
    "SmtpSettings",
    "fill_secrets",
    "secret_slot",
    "send_best_effort",
]

logger = logging.getLogger("jutsu.email")


def secret_slot(name: str) -> str:
    """The placeholder a template writes where a one-time value belongs.

    Public, and the only definition. `jutsu_api.emails` derives its `CODE_SLOT` and
    `TOKEN_SLOT` from this rather than spelling the brackets a second time — a template
    whose placeholder disagreed with the substitution by one character would render an
    email reading `[[code]]` where the code should be, and the failure would only ever
    be seen by a customer.

    Double brackets rather than `{}`: these templates are mostly CSS, and `str.format`
    over a string full of braces is a rendering failure waiting for the one that was not
    escaped.
    """
    return f"[[{name}]]"


def fill_secrets(template: str, secrets: Mapping[str, str], *, as_html: bool) -> tuple[str, str]:
    """Substitute one-time values into a rendered template.

    Returns the filled text and a block holding any secret the template did not
    reference. **A secret is never silently dropped**: an email that promises a code and
    carries none locks its recipient out, and the failure is invisible from the sending
    side. Whatever is left over is appended rather than discarded, which keeps the old
    behaviour — bodies with no placeholders at all got the values appended — working
    unchanged.

    `as_html` escapes on the way in. The values in practice are six digits and
    `secrets.token_urlsafe` output, neither of which contains a character that needs it;
    the escaping is here so that stays true of a value chosen later rather than by
    coincidence of the current generators.
    """
    filled = template
    unused: list[str] = []

    for name, value in secrets.items():
        slot = secret_slot(name)
        rendered = escape(value, quote=True) if as_html else value
        if slot in filled:
            filled = filled.replace(slot, rendered)
        else:
            unused.append(f"{name}: {rendered}")

    return filled, "\n".join(unused)


@dataclass(frozen=True, slots=True)
class InlineImage:
    """An image carried inside the message rather than fetched from a URL.

    Attached as a `multipart/related` part and referenced as `cid:`. That is not a
    stylistic choice: Outlook blocks remote images by default and Gmail blocks them for
    senders the reader has not corresponded with, so a branded authentication email that
    loads its logo over https arrives unbranded — which is the visual signature of the
    phishing it is trying not to resemble. A related part is not a fetch, so it renders
    on first open in every client.
    """

    cid: str
    filename: str
    subtype: str
    #: Out of `repr` for the same reason the secrets are: nobody wants a base64 blob in
    #: an exception message.
    data: bytes = field(repr=False, default=b"")


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    #: The plain-text alternative. Always present, never derived from the HTML: a
    #: `multipart/alternative` with no text part is a strong spam signal, and this is
    #: mail that has to arrive — a sign-in code in a junk folder is a locked-out
    #: customer.
    body: str
    #: The rendered branded document, or None for a message that is genuinely text.
    html: str | None = None
    #: Never logged, never rendered anywhere but the body. Carried separately so a
    #: transport cannot accidentally include it in a diagnostic dump of the message.
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    #: Related parts the HTML references by `cid:`. Empty for a text-only message.
    inline_images: tuple[InlineImage, ...] = ()


class EmailSender(Protocol):
    """One method, so a real provider, a queue and a test double are interchangeable."""

    async def send(self, message: EmailMessage) -> None: ...


async def send_best_effort(sender: EmailSender, message: EmailMessage) -> bool:
    """Deliver a message whose failure must not undo the work that produced it.

    **Only for the welcome messages, and the distinction is whether anyone is blocked.**
    A one-time code that cannot be delivered has to fail its request loudly: somebody is
    sitting in front of a form waiting for it, and a 202 they can never act on is worse
    than an error they can retry. A welcome carries no credential and nobody is waiting
    on it.

    What makes this the right trade rather than a swallowed error is what the failure
    would otherwise take with it. These sends happen inside the request transaction that
    creates an organisation or admits an employee, so an exception here rolls that back —
    and the registrant cannot simply try again, because the challenge and the staged
    payload were both consumed on the way in. A refused SMTP connection would destroy a
    tenant that was successfully created.

    Returns whether it went, so a caller that wants to react can; the log line records
    only that one failed (§4.9 — no address, no subject, no recipient).
    """
    try:
        await sender.send(message)
    except Exception:
        logger.warning("email_delivery_failed")
        return False
    return True


class ConsoleEmailSender:
    """Development transport.

    Writes the message to stdout so a developer can complete a sign-in without an email
    provider configured. This is the one context where printing the code is correct —
    there is no inbox to reach — and it is why the class is named for its output rather
    than pretending to be a mail transport.

    Prints the text alternative, not the HTML. A terminal is not a mail client, and 20KB
    of table markup scrolling past would bury the code this exists to show.

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
        body, leftover = fill_secrets(message.body, message.secrets, as_html=False)
        print(
            f"\n--- email ({self._environment}) ---\n"
            f"to: {message.to}\nsubject: {message.subject}\n\n{body}\n"
            f"{leftover}\n--- end ---\n",
            flush=True,
        )
        # The log line records that a message was sent and to nothing else. No address,
        # no code, no link.
        logger.info("email_sent")


class RecordingEmailSender:
    """Test double. Keeps messages in memory so a test can read the code it must submit.

    Exists so the auth tests exercise the real end-to-end path — issue, deliver, verify —
    rather than reaching into the database for a hash they cannot reverse.

    Stores the message *unrendered*, so `secrets` reads the way it always has and the
    templates can be asserted against with their placeholders intact. A test that wants
    the delivered document runs `fill_secrets` itself, or renders through
    `SmtpEmailSender._render` when the MIME structure is the subject.
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
        text, leftover = fill_secrets(message.body, message.secrets, as_html=False)
        if leftover:
            text = f"{text}\n\n{leftover}\n"

        mime = MimeMessage()
        mime["From"] = self._settings.sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        # An automated one-time code should not generate an out-of-office reply, and it
        # should not be filed as a conversation to reply into.
        mime["Auto-Submitted"] = "auto-generated"
        mime.set_content(text)

        if message.html is None:
            return mime

        html, html_leftover = fill_secrets(message.html, message.secrets, as_html=True)
        if html_leftover:
            # Should never fire — every branded template references every secret it is
            # sent with. It exists so that a template edit which drops a placeholder
            # degrades to an ugly line rather than to a code that never arrives.
            html = html.replace("</body>", f"<pre>{escape(html_leftover)}</pre></body>")

        # text/plain first, then text/html: a client renders the *last* alternative it
        # understands, so reversing these serves plain text to everything.
        mime.add_alternative(html, subtype="html")

        html_part = mime.get_body(preferencelist=("html",))
        if message.inline_images and html_part is not None:
            # The HTML part, not the message — `add_related` here is what turns that one
            # part into `multipart/related`. Attaching to the top level instead produces
            # a message whose text alternative appears to have an image bolted to it,
            # which several clients render as an attachment paperclip on a sign-in mail.
            #
            # `get_body` rather than indexing the payload: the index is only stable while
            # the structure is exactly [plain, html], and the next thing anyone adds here
            # would silently attach the logo to the plain-text part.
            for image in message.inline_images:
                html_part.add_related(
                    image.data,
                    maintype="image",
                    subtype=image.subtype,
                    cid=f"<{image.cid}>",
                    filename=image.filename,
                    disposition="inline",
                )

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
