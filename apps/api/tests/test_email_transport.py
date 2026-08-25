"""The mail transport, and the two things it must never get wrong.

Passwordless sign-in puts email on the critical path for every authentication, so a
transport that silently fails or silently leaks is not a detail. These run without a
network: `_render` is pure, and delivery is exercised against a fake SMTP.
"""

from __future__ import annotations

import asyncio

import pytest
from jutsu_api.config import MissingSecret, _smtp_settings
from jutsu_api.email import ConsoleEmailSender, EmailMessage, SmtpEmailSender, SmtpSettings

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

    def test_console_still_refuses_production(self) -> None:
        with pytest.raises(RuntimeError, match="cannot be used in production"):
            ConsoleEmailSender(environment="prod")
