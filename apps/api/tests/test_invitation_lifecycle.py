"""Cancelling an invitation, and sending a fresh one — over the wire, against real RLS.

`invitations.revoked_at` existed from migration 0002 and nothing could ever write it:
`list_invitations` derived a `revoked` status nobody could reach, and an invitation sent to
the wrong address stood for seventy-two hours with no way to withdraw it.

The two properties worth pinning are the ones a careless implementation gets wrong:

  * **A resend kills the old token.** Re-delivering the same one would extend a live
    credential's life on every press, and leave two working copies in two inboxes if the
    first message merely arrived late.
  * **A resend re-checks the rank ceiling against whoever pressed it.** Otherwise an HR
    Admin could resend a Super Admin's invitation at Super Admin level — an escalation
    with no new code path, only a button.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from sqlalchemy import text
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


async def register_owner(client: AsyncClient, mailbox: RecordingEmailSender) -> None:
    await client.post("/v1/orgs/register", json=REGISTRATION)
    delivered = mailbox.last.secrets
    response = await client.post(
        "/v1/orgs/register/verify",
        json={"token": delivered["token"], "code": delivered["code"]},
    )
    assert response.status_code == 200, response.text


async def invite(client: AsyncClient, *, email: str, role: str = "member") -> None:
    response = await client.post(
        "/v1/employees/invitations",
        json={"email": email, "role": role},
        headers=csrf(client),
    )
    assert response.status_code == 202, response.text


async def only_invitation(client: AsyncClient) -> dict[str, Any]:
    """The single invitation on the list. For the state BEFORE any resend."""
    listed = await client.get("/v1/invitations")
    assert listed.status_code == 200, listed.text
    items: list[dict[str, Any]] = listed.json()["items"]
    assert len(items) == 1, f"expected one invitation, found {len(items)}"
    return items[0]


async def live_invitation(client: AsyncClient) -> dict[str, Any]:
    """The one invitation still waiting.

    A resend deliberately leaves TWO rows — the revoked original and its replacement —
    because the revoked one is the record that JUTSU tried. So "the invitation" after a
    resend has to be selected by state rather than by being the only row, and the
    assertion that exactly one is live is itself worth making: the partial unique index
    permits no more.
    """
    listed = await client.get("/v1/invitations")
    assert listed.status_code == 200, listed.text
    items: list[dict[str, Any]] = listed.json()["items"]
    live = [row for row in items if row["status"] == "pending"]
    assert len(live) == 1, f"expected one live invitation, found {len(live)} of {len(items)}"
    return live[0]


class TestCancelling:
    async def test_it_cancels_a_waiting_invitation_and_names_the_address(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite(client, email="mistake@example.com")
        invitation = await only_invitation(client)

        cancelled = await client.post(
            f"/v1/invitations/{invitation['id']}/revoke", headers=csrf(client)
        )

        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json() == {"email": "mistake@example.com"}
        assert (await only_invitation(client))["status"] == "revoked"

    async def test_the_cancelled_link_stops_working(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The point of cancelling. A status change that left the token live would be
        theatre — whoever holds the link is who becomes the account."""
        await register_owner(client, mailbox)
        await invite(client, email="mistake@example.com")
        token = mailbox.last.secrets["token"]
        invitation = await only_invitation(client)
        await client.post(f"/v1/invitations/{invitation['id']}/revoke", headers=csrf(client))

        accepted = await client.post(
            "/v1/invitations/accept", json={"token": token, "full_name": "Not Invited"}
        )

        assert accepted.status_code == 401, accepted.text

    async def test_it_records_who_cancelled_it(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        await invite(client, email="mistake@example.com")
        invitation = await only_invitation(client)

        await client.post(f"/v1/invitations/{invitation['id']}/revoke", headers=csrf(client))

        rows = (
            await inspector.execute(
                text(
                    "SELECT resource_id, outcome FROM audit_log "
                    "WHERE action = 'member.invite_revoked'"
                )
            )
        ).all()
        assert [(row.resource_id, row.outcome) for row in rows] == [(invitation["id"], "success")]

    async def test_cancelling_twice_is_refused_rather_than_reported_as_done(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        # There is nothing left to cancel, and answering 200 would suggest something
        # happened — which matters when two administrators are looking at the same page.
        await register_owner(client, mailbox)
        await invite(client, email="mistake@example.com")
        invitation = await only_invitation(client)
        await client.post(f"/v1/invitations/{invitation['id']}/revoke", headers=csrf(client))

        again = await client.post(
            f"/v1/invitations/{invitation['id']}/revoke", headers=csrf(client)
        )

        assert again.status_code == 404, again.text

    async def test_an_accepted_invitation_cannot_be_cancelled(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """That person is a member now. Unmaking a membership is deactivation, which is a
        different act under a different permission — not this button."""
        await register_owner(client, mailbox)
        await invite(client, email="joined@example.com")
        invitation = await only_invitation(client)
        accepted = await client.post(
            "/v1/invitations/accept",
            json={"token": mailbox.last.secrets["token"], "full_name": "Charles Babbage"},
        )
        assert accepted.status_code == 200, accepted.text

        # The client is now signed in as the invitee, who holds no `member:invite` — so
        # this is a 403 before it is anything else, which is itself the right answer.
        refused = await client.post(
            f"/v1/invitations/{invitation['id']}/revoke", headers=csrf(client)
        )

        assert refused.status_code == 403, refused.text


class TestResending:
    async def test_it_sends_a_new_link_and_kills_the_old_one(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite(client, email="lost@example.com")
        first = mailbox.last.secrets["token"]
        invitation = await only_invitation(client)

        resent = await client.post(
            f"/v1/invitations/{invitation['id']}/resend", headers=csrf(client)
        )

        assert resent.status_code == 202, resent.text
        second = mailbox.last.secrets["token"]
        assert second != first, "the same credential was re-delivered"

        # The old link is dead. Reusing the token would extend a live credential's life
        # on every press, and leave two working copies if the first mail arrived late.
        stale = await client.post(
            "/v1/invitations/accept", json={"token": first, "full_name": "Charles Babbage"}
        )
        assert stale.status_code == 401, stale.text

    async def test_the_new_link_works(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite(client, email="lost@example.com")
        invitation = await only_invitation(client)
        await client.post(f"/v1/invitations/{invitation['id']}/resend", headers=csrf(client))

        accepted = await client.post(
            "/v1/invitations/accept",
            json={"token": mailbox.last.secrets["token"], "full_name": "Charles Babbage"},
        )

        assert accepted.status_code == 200, accepted.text

    async def test_it_keeps_the_role_the_invitation_was_for(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite(client, email="lost@example.com", role="analyst")
        invitation = await only_invitation(client)

        await client.post(f"/v1/invitations/{invitation['id']}/resend", headers=csrf(client))

        # One live invitation, at the role the original was for. A resend that silently
        # reset somebody to Member would be a permission change wearing a retry.
        assert (await live_invitation(client))["role"] == "analyst"

    async def test_only_one_invitation_stays_live(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        # `uq_invitations_org_email_live` permits exactly one, so a resend that failed to
        # revoke the original would raise rather than quietly leaving two.
        await register_owner(client, mailbox)
        await invite(client, email="lost@example.com")
        invitation = await only_invitation(client)

        await client.post(f"/v1/invitations/{invitation['id']}/resend", headers=csrf(client))

        counts = (
            await inspector.execute(
                text(
                    "SELECT count(*) FILTER (WHERE revoked_at IS NULL) AS live, count(*) AS total "
                    "FROM invitations WHERE lower(email) = 'lost@example.com'"
                )
            )
        ).one()
        assert (counts.live, counts.total) == (1, 2)

    async def test_resending_a_dead_invitation_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite(client, email="lost@example.com")
        invitation = await only_invitation(client)
        await client.post(f"/v1/invitations/{invitation['id']}/revoke", headers=csrf(client))

        # A revoked invitation is still resendable — that is the point of keeping the row
        # — but a *nonexistent* one is not.
        missing = await client.post(
            "/v1/invitations/00000000-0000-4000-8000-000000000000/resend",
            headers=csrf(client),
        )

        assert missing.status_code == 404, missing.text


class TestNeitherRouteWidensAnything:
    async def test_a_member_cannot_reach_either(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite(client, email="member@example.com")
        invitation = await only_invitation(client)
        accepted = await client.post(
            "/v1/invitations/accept",
            json={"token": mailbox.last.secrets["token"], "full_name": "Charles Babbage"},
        )
        assert accepted.status_code == 200, accepted.text

        cancelled = await client.post(
            f"/v1/invitations/{invitation['id']}/revoke", headers=csrf(client)
        )
        resent = await client.post(
            f"/v1/invitations/{invitation['id']}/resend", headers=csrf(client)
        )

        assert cancelled.status_code == 403, cancelled.text
        assert resent.status_code == 403, resent.text

    async def test_neither_answers_without_a_session(self, client: AsyncClient) -> None:
        target = "/v1/invitations/00000000-0000-4000-8000-000000000000"

        assert (await client.post(f"{target}/revoke")).status_code == 401
        assert (await client.post(f"{target}/resend")).status_code == 401


class TestTheRankCeilingAppliesToCancellingToo:
    async def test_an_admin_cannot_cancel_an_invitation_above_their_own_level(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        """`member:invite` is held by HR Admin and IT Admin, not just the top two roles.

        Without a ceiling here, an HR Admin could withdraw a Super Admin invitation the
        Owner had issued — a role they could not have granted and could not change once
        it was accepted. Cancelling somebody's pending access is a smaller act than
        granting it, but it is still an act against a rank above your own.
        """
        await register_owner(client, mailbox)
        # The Owner invites a Super Admin, and separately an HR Admin who will try to
        # cancel it.
        await invite(client, email="deputy@example.com", role="super_admin")
        deputy_invitation = (await client.get("/v1/invitations")).json()["items"][0]
        await invite(client, email="hr@example.com", role="hr_admin")
        hr_token = mailbox.last.secrets["token"]

        accepted = await client.post(
            "/v1/invitations/accept",
            json={"token": hr_token, "full_name": "Henry Ross"},
        )
        assert accepted.status_code == 200, accepted.text

        refused = await client.post(
            f"/v1/invitations/{deputy_invitation['id']}/revoke", headers=csrf(client)
        )

        assert refused.status_code == 403, refused.text
        # And the refusal rolled the speculative UPDATE back rather than leaving the
        # invitation cancelled behind a 403.
        still_live = (
            await inspector.execute(
                text(
                    "SELECT count(*) FROM invitations WHERE lower(email) = 'deputy@example.com' "
                    "AND revoked_at IS NULL AND accepted_at IS NULL"
                )
            )
        ).scalar_one()
        assert still_live == 1, "the invitation was cancelled despite the refusal"

    async def test_no_audit_row_is_written_for_a_refused_cancellation(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        await invite(client, email="deputy@example.com", role="super_admin")
        deputy_invitation = (await client.get("/v1/invitations")).json()["items"][0]
        await invite(client, email="hr@example.com", role="hr_admin")
        await client.post(
            "/v1/invitations/accept",
            json={"token": mailbox.last.secrets["token"], "full_name": "Henry Ross"},
        )

        await client.post(f"/v1/invitations/{deputy_invitation['id']}/revoke", headers=csrf(client))

        written = (
            await inspector.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'member.invite_revoked'")
            )
        ).scalar_one()
        assert written == 0

    async def test_an_admin_can_still_cancel_an_invitation_below_their_level(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        # The ceiling must not lock administrators out of the ordinary case.
        await register_owner(client, mailbox)
        await invite(client, email="junior@example.com", role="member")
        target = (await client.get("/v1/invitations")).json()["items"][0]
        await invite(client, email="hr@example.com", role="hr_admin")
        await client.post(
            "/v1/invitations/accept",
            json={"token": mailbox.last.secrets["token"], "full_name": "Henry Ross"},
        )

        cancelled = await client.post(
            f"/v1/invitations/{target['id']}/revoke", headers=csrf(client)
        )

        assert cancelled.status_code == 200, cancelled.text
