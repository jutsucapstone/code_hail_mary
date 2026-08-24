"""Runtime settings.

Every secret here comes from the environment, which in staging and production means
Secret Manager (§4.10). Nothing has a usable default: `email_pepper` deliberately raises
rather than falling back, because a default pepper is the same as no pepper — every
deployment would derive identical HMACs and the org-less identity table would become
correlatable across installations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from jutsu_api.email import SmtpSettings

__all__ = ["Settings", "get_settings"]

#: Six digits over a 10-symbol alphabet is a 1,000,000-wide space. With the attempt
#: budget below, an attacker's chance of guessing a specific code is 5/1e6 = 5e-6 — and
#: because `auth.consume_attempt` spends the budget atomically, that bound holds under
#: concurrency instead of collapsing to one attempt per round.
OTP_DIGITS: Final = 6
OTP_MAX_ATTEMPTS: Final = 5

#: Short, because an OTP sits in an inbox. The magic-link token shares the challenge row
#: and therefore the same window; it is high-entropy, so the window is about limiting the
#: interception opportunity rather than about guessing.
CHALLENGE_TTL_SECONDS: Final = 10 * 60

#: Staging a registration sends mail to an address the caller names, so it is an open
#: relay without a budget — and each attempt also writes an identity row and a staged
#: payload. Five in an hour is far above any honest use (a person mistypes once or twice
#: and asks for one resend) and far below what makes bulk mailing worthwhile.
REGISTRATION_BUDGET_LIMIT: Final = 5
REGISTRATION_BUDGET_WINDOW_SECONDS: Final = 60 * 60

#: The documents a registrant accepts, and the versions they accept.
#:
#: Server-side constants, never taken from the request. `extra="forbid"` blocks fields
#: the model does not declare, not values within ones it does — so a declared
#: `terms_version` would let the browser name a document it never rendered, and a stale
#: cached bundle would keep naming it for weeks. Date-stamped rather than semver: the
#: question a dispute asks is "which text was published then", and a date answers it.
TERMS_DOCUMENTS: Final[dict[str, str]] = {
    "terms": "2026-08-20",
    "privacy": "2026-08-20",
}

#: An admin console holding a whole tenant's OAuth tokens. Absolute lifetime is a hard
#: ceiling; idle expiry is what actually protects a shared machine.
SESSION_ABSOLUTE_TTL_SECONDS: Final = 12 * 60 * 60
SESSION_IDLE_TTL_SECONDS: Final = 60 * 60

#: Idle expiry is only rewritten when it has moved by at least this much, so a burst of
#: requests does not turn every read into a write on the sessions row.
SESSION_TOUCH_INTERVAL_SECONDS: Final = 5 * 60


class MissingSecret(RuntimeError):
    """A required secret is absent. Raised at first use, never defaulted."""


#: Gmail's submission endpoint. Overridable, because the transport is provider-agnostic
#: and a pilot may well move to a dedicated sender once volume justifies one.
DEFAULT_SMTP_HOST: Final = "smtp.gmail.com"
DEFAULT_SMTP_PORT: Final = 587


@dataclass(frozen=True, slots=True)
class Settings:
    email_pepper: bytes
    environment: str
    #: None when no transport is configured. Absence is meaningful rather than an error:
    #: development runs happily without one, and production refuses to start without one.
    smtp: SmtpSettings | None = None

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"

    @property
    def cookies_secure(self) -> bool:
        """Always true, including in development.

        The session cookies carry the `__Host-` prefix, and that prefix is only honoured
        when the cookie is also `Secure` — a browser rejects `__Host-` without it
        outright. Making this environment-dependent would therefore not "relax" dev, it
        would stop the cookie being stored at all, and the failure would look like a
        broken login rather than a misconfiguration.

        Plain HTTP on localhost is fine: browsers treat localhost as a trustworthy origin
        and accept `Secure` cookies there. Tying this to `X-Forwarded-Proto` instead was
        rejected — an upstream misconfiguration would then silently downgrade every
        cookie in production.
        """
        return True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    pepper = os.environ.get("JUTSU_EMAIL_PEPPER")
    if not pepper:
        raise MissingSecret(
            "JUTSU_EMAIL_PEPPER is not set. It keys the HMAC that stands in for email "
            "addresses in the org-less auth schema; without it those rows would either "
            "hold plaintext or be identical across deployments. Generate one with "
            '`python -c "import secrets; print(secrets.token_urlsafe(32))"` for local '
            "development, and take it from Secret Manager everywhere else."
        )

    environment = os.environ.get("JUTSU_ENV", "dev")
    return Settings(
        email_pepper=pepper.encode("utf-8"),
        environment=environment,
        smtp=_smtp_settings(environment),
    )


def _smtp_settings(environment: str) -> SmtpSettings | None:
    """The mail transport, or None when there is not one.

    Username and password are the whole configuration: the host and port have sensible
    defaults, and an address is only useful with a credential to send from it.

    **Production refuses to start without one**, rather than falling through to a
    transport that prints to stdout. Passwordless auth puts email on the critical path
    for every sign-in, so a deployment that cannot send mail cannot authenticate anyone —
    and discovering that from a customer is far worse than refusing to boot.
    """
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")

    if not username or not password:
        if environment == "prod":
            raise MissingSecret(
                "SMTP_USERNAME and SMTP_PASSWORD are not set. Passwordless sign-in "
                "cannot deliver a code without a mail transport, so production will not "
                "start without one. Use an application password, never an account "
                "password, and take both from Secret Manager."
            )
        return None

    return SmtpSettings(
        host=os.environ.get("SMTP_HOST", DEFAULT_SMTP_HOST),
        port=int(os.environ.get("SMTP_PORT", DEFAULT_SMTP_PORT)),
        username=username,
        password=password,
        # Falls back to the account being authenticated as, which is what most providers
        # require anyway — Gmail rewrites a From it has not verified.
        sender=os.environ.get("SMTP_FROM", username),
    )
