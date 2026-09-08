"""No one-time secret reaches a log, proven against the flows that mint them.

A one-time secret exists in exactly two places: the message and the database hash. A log
line carrying it puts it in a third — usually the least protected of the three, and the
one most likely to be shipped to an aggregator that keeps it for ninety days. §4.9 forbids
PII in logs; this is the stronger case, because the value is a live credential.

Every module that touches one already says so in its docstring, which is the problem:
"nothing here logs a code" is a claim about code somebody may change, and a `logger.info`
added while debugging a delivery failure would be invisible in review and catastrophic in
production. So it is asserted here, over the real routes, against the real logger — a
registration code, a verification token, an invitation token, and a whole bulk batch of
them.

**The assertion is on the value, not on the shape of a line.** A test that looked for
`"code"` as a substring would pass against a line reading `code=418293`, and a test that
allow-listed known log events would pass against a new one nobody added to the list.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from sqlalchemy.ext.asyncio import AsyncSession

REGISTRATION = {
    "full_name": "Ada Lovelace",
    "work_email": "ada@example.com",
    "company_name": "Example Analytical",
    "company_domain": "example.com",
    "job_title": "Head of Engineering",
    "org_size": "51-200",
    "terms_accepted": True,
}


@pytest.fixture
async def client(
    db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as http:
        yield http


def csrf(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


def every_secret(mailbox: RecordingEmailSender) -> set[str]:
    """Every one-time value the transport was handed, across every message."""
    return {value for message in mailbox.messages for value in message.secrets.values() if value}


async def test_no_secret_from_any_flow_appears_in_any_log_line(
    client: AsyncClient,
    mailbox: RecordingEmailSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Register, verify, invite one, invite twelve — then read back everything logged."""
    # DEBUG, deliberately: a value leaked at a level the deployment does not currently
    # emit is still a value in the code, and the level is one environment variable away
    # from being turned on while somebody debugs a delivery problem.
    caplog.set_level(logging.DEBUG)

    registered = await client.post("/v1/orgs/register", json=REGISTRATION)
    assert registered.status_code == 202, registered.text
    delivered = mailbox.last.secrets

    verified = await client.post(
        "/v1/orgs/register/verify",
        json={"token": delivered["token"], "code": delivered["code"]},
    )
    assert verified.status_code == 200, verified.text

    single = await client.post(
        "/v1/employees/invitations",
        json={"email": "one@example.com", "role": "member"},
        headers=csrf(client),
    )
    assert single.status_code == 202, single.text

    batch = await client.post(
        "/v1/employees/invitations/bulk",
        json={"rows": [{"email": f"person{n}@example.com", "role": "member"} for n in range(12)]},
        headers=csrf(client),
    )
    assert batch.status_code == 202, batch.text
    assert batch.json()["sent"] == 12

    secrets = every_secret(mailbox)
    # Fourteen: one registration token, one six-digit code, and twelve-plus-one
    # invitation tokens. Asserted so a flow that silently stopped minting one cannot make
    # this test vacuous.
    assert len(secrets) == 15, f"expected 15 secrets, minted {len(secrets)}"

    logged = caplog.text
    leaked = sorted(secret for secret in secrets if secret in logged)
    assert leaked == [], f"{len(leaked)} one-time secret(s) reached a log line"


async def test_no_email_address_appears_in_a_log_line_either(
    client: AsyncClient,
    mailbox: RecordingEmailSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§4.9's other half, over the route most likely to break it.

    A bulk import is where an address is most tempting to log — twelve rows, one of which
    failed, and the obvious diagnostic is "which one". The answer is that the caller gets
    the row back in the response, and the log gets an event name and a count.
    """
    caplog.set_level(logging.DEBUG)

    await client.post("/v1/orgs/register", json=REGISTRATION)
    delivered = mailbox.last.secrets
    await client.post(
        "/v1/orgs/register/verify",
        json={"token": delivered["token"], "code": delivered["code"]},
    )

    addresses = [f"person{n}@example.com" for n in range(6)]
    sent = await client.post(
        "/v1/employees/invitations/bulk",
        json={"rows": [{"email": address, "role": "member"} for address in addresses]},
        headers=csrf(client),
    )
    assert sent.status_code == 202, sent.text

    logged = caplog.text
    leaked = sorted(address for address in [*addresses, "ada@example.com"] if address in logged)
    assert leaked == [], f"{len(leaked)} address(es) reached a log line"
