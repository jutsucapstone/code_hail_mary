"""The mail transport, and the two things it must never get wrong.

Passwordless sign-in puts email on the critical path for every authentication, so a
transport that silently fails or silently leaks is not a detail. These run without a
network: `_render` is pure, and delivery is exercised against a fake SMTP.
"""

from __future__ import annotations

import asyncio
from email.utils import parseaddr

import pytest
from jutsu_api.config import MissingSecret, _app_url, _smtp_settings
from jutsu_api.email import (
    ConsoleEmailSender,
    EmailMessage,
    InlineImage,
    SmtpEmailSender,
    SmtpSettings,
    secret_slot,
    send_best_effort,
)

SETTINGS = SmtpSettings(
    host="smtp.example.com",
    port=587,
    username="sender@example.com",
    password="app-password-not-real",
    sender="sender@example.com",
)

MESSAGE = EmailMessage(
    to="ada@example.com",
    subject="Your JUTSU sign-in code",
    body="Your JUTSU sign-in code is below.",
    secrets={"code": "483920", "token": "a-long-opaque-token"},
)


class TestRendering:
    def test_the_code_reaches_the_body(self) -> None:
        """`secrets` travels separately precisely so no transport splices it in by
        accident — but the message itself is the one place it must appear, or the
        recipient gets an email telling them a code is below and no code."""
        mime = SmtpEmailSender(SETTINGS)._render(MESSAGE)
        body = mime.get_content()

        assert "483920" in body
        assert "a-long-opaque-token" in body
        assert "Your JUTSU sign-in code is below." in body

    def test_headers_mark_it_automated(self) -> None:
        """An out-of-office reply to a one-time code helps nobody."""
        mime = SmtpEmailSender(SETTINGS)._render(MESSAGE)

        assert mime["To"] == "ada@example.com"
        assert mime["From"] == "sender@example.com"
        assert mime["Auto-Submitted"] == "auto-generated"

    def test_a_message_with_no_secrets_is_unchanged(self) -> None:
        """The "this address has no account" mail carries none, and must not gain a
        stray blank block."""
        plain = EmailMessage(to="x@example.com", subject="s", body="no secrets here")
        assert SmtpEmailSender(SETTINGS)._render(plain).get_content().strip() == "no secrets here"

    def test_a_placeholder_is_filled_in_place_rather_than_appended(self) -> None:
        """The branded templates carry `[[code]]` where the code belongs, so it lands
        inside the sentence rather than in a block bolted to the end."""
        message = EmailMessage(
            to="ada@example.com",
            subject="s",
            body=f"Your code is {secret_slot('code')}.",
            secrets={"code": "483920"},
        )
        content = SmtpEmailSender(SETTINGS)._render(message).get_content()

        assert content.strip() == "Your code is 483920."


class TestBrandedRendering:
    """The multipart structure a mail client needs before it renders any of this."""

    BRANDED = EmailMessage(
        to="ada@example.com",
        subject="Your JUTSU sign-in code",
        body=f"Your code is {secret_slot('code')}.",
        html=(
            "<!DOCTYPE html><html><body>"
            f'<img src="cid:jutsu-mark" /><p>{secret_slot("code")}</p>'
            f'<a href="https://jutsu.example/pilot/verify?token={secret_slot("token")}">go</a>'
            "</body></html>"
        ),
        secrets={"code": "483920", "token": "a-long-opaque-token"},
        inline_images=(
            InlineImage(
                cid="jutsu-mark", filename="jutsu-mark.png", subtype="png", data=b"\x89PNG"
            ),
        ),
    )

    def _structure(self) -> list[str]:
        mime = SmtpEmailSender(SETTINGS)._render(self.BRANDED)
        return [part.get_content_type() for part in mime.walk()]

    def test_plain_text_comes_before_html(self) -> None:
        """A client renders the *last* alternative it understands. Reversing these
        serves plain text to everything, which is the sort of bug that looks like the
        template never shipped."""
        assert self._structure() == [
            "multipart/alternative",
            "text/plain",
            "multipart/related",
            "text/html",
            "image/png",
        ]

    def test_the_mark_hangs_off_the_html_part_and_not_the_message(self) -> None:
        """`multipart/related` has to wrap the HTML alone. Attaching at the top level
        instead produces a message several clients render with an attachment paperclip
        on it — on a sign-in mail, that reads as suspicious."""
        mime = SmtpEmailSender(SETTINGS)._render(self.BRANDED)
        related = next(p for p in mime.walk() if p.get_content_type() == "multipart/related")
        inside = [p.get_content_type() for p in related.iter_parts()]

        assert inside == ["text/html", "image/png"]
        image = next(p for p in mime.walk() if p.get_content_type() == "image/png")
        assert image["Content-ID"] == "<jutsu-mark>"
        assert image.get_content_disposition() == "inline"

    def test_both_alternatives_carry_the_code(self) -> None:
        """The text part is not decoration — it is what a spam filter scores, and what a
        terminal client and a watch preview actually show."""
        mime = SmtpEmailSender(SETTINGS)._render(self.BRANDED)
        plain = mime.get_body(preferencelist=("plain",))
        html = mime.get_body(preferencelist=("html",))

        assert plain is not None and html is not None
        assert "483920" in plain.get_content()
        assert "483920" in html.get_content()

    def test_no_placeholder_survives_delivery(self) -> None:
        """A leftover slot renders as the literal text `[[code]]` where the code should
        be, and the first person to see it would be a customer."""
        mime = SmtpEmailSender(SETTINGS)._render(self.BRANDED)

        assert "[[" not in mime.as_string()

    def test_a_text_only_message_gains_no_html_part(self) -> None:
        """`multipart/alternative` with one alternative is a structure some clients
        render as an empty message."""
        plain = EmailMessage(to="x@example.com", subject="s", body="text")
        assert SmtpEmailSender(SETTINGS)._render(plain).get_content_type() == "text/plain"


class TestBestEffortDelivery:
    """Welcome mail must never roll back the account it is welcoming somebody to."""

    class Broken:
        async def send(self, message: EmailMessage) -> None:
            raise ConnectionRefusedError("provider is down")

    def test_a_failed_welcome_is_reported_not_raised(self) -> None:
        """This runs inside the transaction that created the tenant. Raising would undo
        an organisation, an owner and a spent JUTSU ID over a bad minute at the mail
        provider — and the registrant could not retry, because the challenge and the
        staged payload are both consumed by then."""
        assert asyncio.run(send_best_effort(self.Broken(), MESSAGE)) is False

    def test_the_failure_log_says_nothing_about_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []

        class Recorder:
            def warning(self, message: str, *args: object, **kwargs: object) -> None:
                said.append(message)

        monkeypatch.setattr("jutsu_api.email.logger", Recorder())
        asyncio.run(send_best_effort(self.Broken(), MESSAGE))

        assert said == ["email_delivery_failed"]

    def test_a_successful_send_reports_it(self) -> None:
        delivered: list[EmailMessage] = []

        class Working:
            async def send(self, message: EmailMessage) -> None:
                delivered.append(message)

        assert asyncio.run(send_best_effort(Working(), MESSAGE)) is True
        assert delivered == [MESSAGE]


class TestDelivery:
    def test_it_logs_that_mail_was_sent_and_nothing_about_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§4.9. The address would put the customer list in the aggregator; the code
        would put a live credential there.

        The module's `logger` is replaced outright rather than captured through
        `caplog` or a handler. Two earlier attempts failed intermittently depending on
        test order: the app's `_configure_logging` does `root.handlers = [handler]` and
        sets levels globally, so anything that had already built the app changed what a
        capture could see. Substituting the object under test removes the dependence on
        global logging state entirely — the assertion is about what this transport says,
        not about how logging happens to be configured.
        """
        sender = SmtpEmailSender(SETTINGS)
        monkeypatch.setattr(sender, "_deliver", lambda _mime: None)

        said: list[str] = []

        class Recorder:
            def info(self, message: str, *args: object, **kwargs: object) -> None:
                said.append(message % args if args else message)

        monkeypatch.setattr("jutsu_api.email.logger", Recorder())
        asyncio.run(sender.send(MESSAGE))

        rendered = " ".join(said)
        assert "email_sent" in rendered
        assert "483920" not in rendered
        assert "a-long-opaque-token" not in rendered
        assert "ada@example.com" not in rendered

    @pytest.mark.parametrize(("port", "starttls"), [(587, True), (2525, True), (465, False)])
    def test_the_port_decides_the_tls_mode(self, port: int, starttls: bool) -> None:
        """465 is TLS from the first byte, 587 upgrades in-band. Getting it backwards
        hangs until the socket times out, which reads like a network fault."""
        settings = SmtpSettings(
            host="h", port=port, username="u", password="p", sender="s@example.com"
        )
        assert settings.uses_starttls is starttls


class TestConfiguration:
    def test_production_refuses_to_start_without_a_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deployment that cannot send mail cannot authenticate anyone. Failing at
        boot is far better than discovering it from a customer."""
        monkeypatch.delenv("SMTP_USERNAME", raising=False)
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)

        with pytest.raises(MissingSecret, match="SMTP_USERNAME"):
            _smtp_settings("prod")

    def test_development_is_happy_without_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SMTP_USERNAME", raising=False)
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)

        assert _smtp_settings("dev") is None

    def test_the_sender_defaults_to_the_authenticated_account(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Right for a provider that authenticates as a mailbox: Gmail rewrites a From it
        has not verified, so guessing a different one produces mail that appears to come
        from somewhere else."""
        monkeypatch.setenv("SMTP_USERNAME", "someone@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "app-password")
        monkeypatch.delenv("SMTP_FROM", raising=False)

        settings = _smtp_settings("prod")
        assert settings is not None
        assert settings.sender == "someone@example.com"
        assert settings.host == "smtp.resend.com"
        assert settings.port == 587

    def test_a_username_that_is_not_an_address_demands_an_explicit_from(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bug this exists to prevent, from a real deployment.

        Resend authenticates as the literal string `resend`, not as a mailbox. The
        fallback above then yields a From header of `resend`, which the provider rejects
        — but only at the moment of the first send, long after the service has started,
        passed its health check and told a registrant to check their email. Refusing at
        construction turns a silent runtime failure into a boot failure that names the
        variable to set.
        """
        monkeypatch.setenv("SMTP_USERNAME", "resend")
        monkeypatch.setenv("SMTP_PASSWORD", "re_not_a_real_key")
        monkeypatch.delenv("SMTP_FROM", raising=False)

        with pytest.raises(MissingSecret, match="SMTP_FROM"):
            _smtp_settings("prod")

    def test_an_api_key_provider_is_fine_once_from_is_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SMTP_USERNAME", "resend")
        monkeypatch.setenv("SMTP_PASSWORD", "re_not_a_real_key")
        monkeypatch.setenv("SMTP_FROM", "noreply@jutsu.co.in")

        settings = _smtp_settings("prod")
        assert settings is not None
        assert settings.sender == "noreply@jutsu.co.in"
        assert settings.username == "resend"

    def test_a_display_name_is_carried_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """What production actually sends as.

        A recipient sees the sender column of their mail client, and a one-time code
        arriving from a bare `noreply@` reads like something to be suspicious of.
        """
        monkeypatch.setenv("SMTP_USERNAME", "resend")
        monkeypatch.setenv("SMTP_PASSWORD", "re_not_a_real_key")
        monkeypatch.setenv("SMTP_FROM", "JUTSU <noreply@jutsu.co.in>")

        settings = _smtp_settings("prod")
        assert settings is not None
        assert settings.sender == "JUTSU <noreply@jutsu.co.in>"

    def test_the_display_name_stays_out_of_the_envelope(self) -> None:
        """The name is presentation; the envelope sender is the address alone.

        `smtplib.send_message` derives `MAIL FROM` by parsing the From header, so this
        asserts against the same parse rather than trusting that it does. If the display
        name leaked into the envelope, SPF would be checked against a malformed sender
        and every message would fail alignment — visible only as deliverability, which
        is the slowest possible way to find out.
        """
        settings = SmtpSettings(
            host="smtp.resend.com",
            port=587,
            username="resend",
            password="re_not_a_real_key",
            sender="JUTSU <noreply@jutsu.co.in>",
        )
        mime = SmtpEmailSender(settings)._render(MESSAGE)

        assert mime["From"] == "JUTSU <noreply@jutsu.co.in>"
        assert parseaddr(mime["From"]) == ("JUTSU", "noreply@jutsu.co.in")
        # And the recipient is untouched by any of it.
        assert mime["To"] == "ada@example.com"

    @pytest.mark.parametrize(
        "broken",
        [
            "JUTSU noreply@jutsu.co.in",  # display form, angle brackets forgotten
            "JUTSU <>",  # name but no address
            "resend",  # the provider username, not an address
            "",  # explicitly blank
        ],
    )
    def test_an_unusable_from_is_refused_at_boot(
        self, broken: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The near-misses matter more than the obvious ones.

        `JUTSU noreply@jutsu.co.in` contains an "@" and passed the original check, which
        only scanned for one — but it parses as a single address with spaces in it, which
        the provider rejects at the first send, long after the service reported healthy
        and told a registrant to check their email.
        """
        monkeypatch.setenv("SMTP_USERNAME", "resend")
        monkeypatch.setenv("SMTP_PASSWORD", "re_not_a_real_key")
        monkeypatch.setenv("SMTP_FROM", broken)

        with pytest.raises(MissingSecret, match="SMTP_FROM"):
            _smtp_settings("prod")

    def test_console_still_refuses_production(self) -> None:
        with pytest.raises(RuntimeError, match="cannot be used in production"):
            ConsoleEmailSender(environment="prod")


class TestAppUrl:
    """Where the links in outbound mail point.

    Defaulted per environment rather than required, because a missing value cannot be
    caught at boot the way a missing SMTP credential can — the service would start,
    authenticate people, and only produce a broken link in mail nobody on the team reads.
    """

    def test_production_defaults_to_the_deployed_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JUTSU_APP_URL", raising=False)
        assert _app_url("prod") == "https://jutsu.co.in"

    def test_development_defaults_to_the_dev_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Port 3210, not 3000."""
        monkeypatch.delenv("JUTSU_APP_URL", raising=False)
        assert _app_url("dev") == "http://localhost:3210"

    @pytest.mark.parametrize(
        "configured", ["https://preview.jutsu.co.in", "https://preview.jutsu.co.in/"]
    )
    def test_a_trailing_slash_is_stripped(
        self, configured: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every template concatenates a path onto this, and `//pilot/verify` is a URL
        some routers answer and some do not — a bug that only appears in the one
        environment where the variable was set by hand."""
        monkeypatch.setenv("JUTSU_APP_URL", configured)
        assert _app_url("prod") == "https://preview.jutsu.co.in"
