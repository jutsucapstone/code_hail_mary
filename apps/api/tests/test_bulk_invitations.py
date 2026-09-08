"""Onboarding a whole team at once.

Three things are worth testing here and the rest follows from them:

  * **The preview writes nothing.** Its entire value is that an administrator sees the
    six people who already have accounts before seventy-two others receive mail. A
    preview with a side effect is not a preview.
  * **One bad row does not take the batch with it.** Postgres aborts a transaction at its
    first error, so a single failing address would otherwise silently discard every
    invitation after it — the batch would report success and send nothing.
  * **Nothing here widens authorization.** Bulk invite takes the same permission as
    single invite and re-checks the rank ceiling per row, so a CSV cannot do what its
    author could not do one address at a time.

Against a real Postgres, because the parts that matter — row-level security scoping the
"already a member" lookup, the partial unique index behind the duplicate refusal, and the
transaction abort the savepoints exist to contain — are all enforced there.
"""

from __future__ import annotations

import base64
import io
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.bulk_invitations import (
    MAX_XLSX_BYTES,
    BulkOutcome,
    BulkRow,
    invite_many,
    parse_csv,
    parse_pasted,
    parse_xlsx,
)
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import EmailMessage, RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from jutsu_core.rbac import Role
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


async def preview(client: AsyncClient, **source: object) -> dict[str, Any]:
    response = await client.post(
        "/v1/employees/invitations/preview", json=source, headers=csrf(client)
    )
    assert response.status_code == 200, response.text
    # Annotated on the way out: `Response.json()` is `Any`, and mypy's `warn_return_any`
    # is what stops that `Any` spreading into every assertion below.
    payload: dict[str, Any] = response.json()
    return payload


async def send_bulk(client: AsyncClient, rows: list[dict[str, Any]]) -> dict[str, Any]:
    response = await client.post(
        "/v1/employees/invitations/bulk", json={"rows": rows}, headers=csrf(client)
    )
    assert response.status_code == 202, response.text
    # Annotated on the way out: `Response.json()` is `Any`, and mypy's `warn_return_any`
    # is what stops that `Any` spreading into every assertion below.
    payload: dict[str, Any] = response.json()
    return payload


def outcomes(payload: dict[str, Any]) -> dict[str, str]:
    """Address to outcome. Only for cases where no address repeats — see the ordered
    comparison in the classification test for the one where they do."""
    return {row["email"]: row["outcome"] for row in payload["rows"]}


# --------------------------------------------------------------------------- parsing


class TestReadingWhatThePersonActuallyPasted:
    """Nobody pastes a clean list. They paste what their mail client gave them."""

    def test_it_splits_on_every_separator_a_person_might_use(self) -> None:
        rows = parse_pasted("a@x.com, b@x.com;c@x.com\nd@x.com\te@x.com")

        assert [row.email for row in rows] == [
            "a@x.com",
            "b@x.com",
            "c@x.com",
            "d@x.com",
            "e@x.com",
        ]

    def test_it_takes_the_address_out_of_a_mail_client_contact(self) -> None:
        # The failure this prevents: splitting on whitespace turns one contact into three
        # rows, two of which are "Ada" and "Lovelace" — a preview full of invented errors
        # is a preview nobody reads.
        rows = parse_pasted('Ada Lovelace <ada@example.com>, "Babbage, C" <cb@example.com>')

        assert [row.email for row in rows] == ["ada@example.com", "cb@example.com"]

    def test_it_keeps_the_unreadable_ones_rather_than_dropping_them(self) -> None:
        # Silently discarding a malformed line is how somebody never gets invited and
        # nobody finds out. It must survive to be shown as invalid.
        rows = parse_pasted("good@x.com\nnot-an-address\n")

        assert [row.email for row in rows] == ["good@x.com", "not-an-address"]

    def test_it_carries_the_chosen_default_role(self) -> None:
        rows = parse_pasted("a@x.com", default_role=Role.VIEWER)

        assert rows[0].role is Role.VIEWER


class TestReadingASpreadsheet:
    def test_it_honours_a_header_and_ignores_columns_it_has_no_field_for(self) -> None:
        # A real HR export carries twenty columns this product cannot store. Refusing the
        # file over them would make the feature unusable with the data people have.
        rows = parse_csv(
            "email,role,role_title,cost_centre,start_date\n"
            "a@x.com,it_admin,Platform Lead,CC-12,2026-01-04\n"
        )

        assert rows[0].email == "a@x.com"
        assert rows[0].role is Role.IT_ADMIN
        assert rows[0].role_title == "Platform Lead"

    def test_a_file_with_no_header_is_a_column_of_addresses(self) -> None:
        # Which is what a list saved out of a spreadsheet actually looks like.
        rows = parse_csv("a@x.com\nb@x.com\n")

        assert [row.email for row in rows] == ["a@x.com", "b@x.com"]

    def test_an_unrecognised_role_is_reported_and_never_downgraded(self) -> None:
        # The dangerous alternative is seating somebody as a Member because their
        # spreadsheet said "Manager", which nobody would notice until it mattered.
        rows = parse_csv("email,role\na@x.com,Manager\n")

        assert rows[0].role_error == "Manager"

    def test_it_reads_the_workbook_an_hr_team_already_has(self) -> None:
        openpyxl = pytest.importorskip("openpyxl")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["email", "role", "role_title"])
        sheet.append(["a@x.com", "member", "Analyst, Risk"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        rows = parse_xlsx(buffer.getvalue())

        assert rows[0].email == "a@x.com"
        # Quoted on the way through the CSV reader, so a comma inside a cell stays one
        # value instead of becoming a second column.
        assert rows[0].role_title == "Analyst, Risk"

    def test_it_refuses_a_file_that_is_not_a_workbook(self) -> None:
        from jutsu_core.errors import ValidationFailed

        with pytest.raises(ValidationFailed):
            parse_xlsx(b"%PDF-1.7 this is not a spreadsheet")

    def test_it_refuses_an_oversized_file_before_opening_it(self) -> None:
        from jutsu_core.errors import ValidationFailed

        # Refused on length, so no untrusted bytes reach a container-format parser at all.
        with pytest.raises(ValidationFailed):
            parse_xlsx(b"\x00" * (MAX_XLSX_BYTES + 1))


# --------------------------------------------------------------------------- preview


class TestThePreviewTellsTheTruthAndChangesNothing:
    async def test_it_sorts_every_row_into_the_outcome_the_admin_can_act_on(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        invited = await client.post(
            "/v1/employees/invitations",
            json={"email": "waiting@example.com", "role": "member"},
            headers=csrf(client),
        )
        assert invited.status_code == 202, invited.text

        result = await preview(
            client,
            emails=(
                "fresh@example.com\n"
                "ada@example.com\n"  # the owner, an active member
                "waiting@example.com\n"  # already has a live invitation
                "FRESH@example.com\n"  # the same address again, differently cased
                "not-an-address\n"
            ),
        )

        # Compared in order, and as pairs: the same address appears twice on purpose, so
        # a dict keyed by address would hide exactly the row this is about. Every line
        # the person pasted comes back, in the order they pasted it, so the preview lines
        # up with the textarea they are looking at.
        assert [(row["email"], row["outcome"]) for row in result["rows"]] == [
            ("fresh@example.com", BulkOutcome.READY),
            ("ada@example.com", BulkOutcome.ALREADY_MEMBER),
            ("waiting@example.com", BulkOutcome.ALREADY_INVITED),
            # Normalised, so a second casing of an address already in the list is the
            # duplicate — the database would have refused it as one anyway.
            ("fresh@example.com", BulkOutcome.DUPLICATE),
            ("not-an-address", BulkOutcome.INVALID_EMAIL),
        ]
        assert result["ready"] == 1
        assert result["total"] == 5

    async def test_it_sends_nothing_and_writes_nothing(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        before = len(mailbox.messages)

        await preview(client, emails="one@example.com two@example.com three@example.com")

        assert len(mailbox.messages) == before, "the preview delivered a message"
        rows = (await inspector.execute(text("SELECT count(*) FROM invitations"))).scalar_one()
        assert rows == 0, "the preview created an invitation"

    async def test_it_refuses_a_role_the_inviter_does_not_outrank(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        # The owner is the top of the ladder, so `owner` is the one role they cannot
        # confer — the ceiling is strict, not "at or below".
        result = await preview(client, csv="email,role\nnew@example.com,owner\n")

        assert outcomes(result) == {"new@example.com": BulkOutcome.ROLE_TOO_HIGH}
        assert result["ready"] == 0

    async def test_it_refuses_more_addresses_than_it_will_import(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        response = await client.post(
            "/v1/employees/invitations/preview",
            json={"emails": "\n".join(f"p{n}@example.com" for n in range(201))},
            headers=csrf(client),
        )

        # 422, the envelope's `validation_failed`, and the message names the count — an
        # administrator who pasted the whole company needs to know how far over they are.
        assert response.status_code == 422, response.text
        assert "201" in response.json()["error"]["message"]

    async def test_it_refuses_two_sources_at_once(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        # Two sources would disagree about the roles, and the administrator would never
        # see which one had been used.
        await register_owner(client, mailbox)
        response = await client.post(
            "/v1/employees/invitations/preview",
            json={"emails": "a@x.com", "csv": "email\nb@x.com\n"},
            headers=csrf(client),
        )

        assert response.status_code == 422, response.text

    async def test_it_reads_a_workbook_posted_as_base64(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        openpyxl = pytest.importorskip("openpyxl")
        await register_owner(client, mailbox)
        workbook = openpyxl.Workbook()
        workbook.active.append(["email"])
        workbook.active.append(["sheet@example.com"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        result = await preview(
            client, xlsx_base64=base64.b64encode(buffer.getvalue()).decode("ascii")
        )

        assert outcomes(result) == {"sheet@example.com": BulkOutcome.READY}


# --------------------------------------------------------------------------- sending


class TestSendingTheBatch:
    async def test_it_invites_everyone_and_says_so(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        before = len(mailbox.messages)

        result = await send_bulk(
            client,
            [
                {"email": "one@example.com", "role": "member"},
                {"email": "two@example.com", "role": "viewer"},
                {"email": "three@example.com", "role": "member", "role_title": "Analyst"},
            ],
        )

        assert result["sent"] == 3
        assert result["failed"] == 0
        assert len(mailbox.messages) - before == 3
        stored = (
            await inspector.execute(
                text("SELECT email, role_key, role_title FROM invitations ORDER BY email")
            )
        ).all()
        assert [(row.email, row.role_key) for row in stored] == [
            ("one@example.com", "member"),
            ("three@example.com", "member"),
            ("two@example.com", "viewer"),
        ]
        assert stored[1].role_title == "Analyst"

    async def test_every_invitation_carries_its_own_token(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        # One token reused across a batch would let any recipient accept as any other.
        await register_owner(client, mailbox)
        before = len(mailbox.messages)

        await send_bulk(
            client,
            [
                {"email": "one@example.com", "role": "member"},
                {"email": "two@example.com", "role": "member"},
            ],
        )

        tokens = [message.secrets["token"] for message in mailbox.messages[before:]]
        assert len(set(tokens)) == 2

    async def test_re_sending_the_same_list_emails_nobody_twice(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        # The retry path. An administrator whose connection dropped mid-request presses
        # the button again, and the people who already have mail must not get a second.
        await register_owner(client, mailbox)
        rows = [
            {"email": "one@example.com", "role": "member"},
            {"email": "two@example.com", "role": "member"},
        ]
        await send_bulk(client, rows)
        before = len(mailbox.messages)

        second = await send_bulk(client, rows)

        assert second["sent"] == 0
        assert outcomes(second) == {
            "one@example.com": BulkOutcome.ALREADY_INVITED,
            "two@example.com": BulkOutcome.ALREADY_INVITED,
        }
        assert len(mailbox.messages) == before

    async def test_an_expired_invitation_does_not_lock_the_address_out_for_ever(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        """The partial index cannot mention `expires_at`, so expiry had to be handled.

        `uq_invitations_org_email_live` is partial on `accepted_at IS NULL AND revoked_at
        IS NULL` — `now()` is not immutable and no index predicate may reference it. An
        invitation that merely ran out of time therefore still occupied the slot, and
        re-inviting that person failed with "already has an invitation waiting", which
        was the one thing that was not true.
        """
        await register_owner(client, mailbox)
        await send_bulk(client, [{"email": "slow@example.com", "role": "member"}])
        await inspector.execute(
            text("UPDATE invitations SET expires_at = now() - interval '1 day'")
        )
        before = len(mailbox.messages)

        again = await send_bulk(client, [{"email": "slow@example.com", "role": "member"}])

        assert outcomes(again) == {"slow@example.com": BulkOutcome.SENT}
        assert len(mailbox.messages) - before == 1
        live = (
            await inspector.execute(
                text(
                    "SELECT count(*) FROM invitations WHERE lower(email) = 'slow@example.com' "
                    "AND revoked_at IS NULL AND accepted_at IS NULL"
                )
            )
        ).scalar_one()
        assert live == 1, "the expired invitation was not retired"

    async def test_a_row_that_aborts_the_transaction_does_not_take_the_batch_with_it(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
        inspector: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The savepoint, tested against the thing it protects against.

        Postgres aborts a transaction at its first error, so a row that fails at the
        database — a race with another administrator inviting the same person, most
        plainly — leaves the connection unusable. Without `begin_nested` every row after
        it fails too, and the batch reports a success it did not have.

        The failure is induced rather than raced, because a race is not a test. What
        matters is that a *database* error, not a Python one, is contained: only a
        savepoint can roll back far enough to keep the connection usable.
        """
        await register_owner(client, mailbox)
        principal_row = (
            await inspector.execute(text("SELECT id, org_id FROM users LIMIT 1"))
        ).one()

        from jutsu_api import bulk_invitations
        from jutsu_api.invitations import invite_employee
        from jutsu_api.security import Principal
        from jutsu_core.rbac import ROLE_PERMISSIONS

        actor = Principal(
            session_id=principal_row.id,
            identity_id=principal_row.id,
            user_id=principal_row.id,
            org_id=principal_row.org_id,
            role=Role.OWNER,
            permissions=ROLE_PERMISSIONS[Role.OWNER],
        )
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(principal_row.org_id)},
        )

        real = invite_employee

        async def failing(session: AsyncSession, **kwargs: Any) -> Any:
            if kwargs["email"] == "poison@example.com":
                # A genuine Postgres error, which is what aborts the transaction. A bare
                # `raise` in Python would leave the connection perfectly healthy and the
                # test would pass with the savepoints removed.
                await session.execute(text("SELECT 1 / 0"))
            return await real(session, **kwargs)

        monkeypatch.setattr(bulk_invitations, "invite_employee", failing)

        result = await invite_many(
            db_session,
            actor=actor,
            rows=[
                BulkRow(email="before@example.com"),
                BulkRow(email="poison@example.com"),
                BulkRow(email="after@example.com"),
            ],
            settings=settings,
            sender=mailbox,
        )

        assert [row.outcome for row in result.rows] == [
            BulkOutcome.SENT,
            BulkOutcome.FAILED,
            BulkOutcome.SENT,
        ]
        await db_session.commit()
        stored = (
            await inspector.execute(text("SELECT email FROM invitations ORDER BY email"))
        ).scalars()
        assert list(stored) == ["after@example.com", "before@example.com"]

    async def test_an_invitation_whose_email_bounces_is_revoked_not_left_live(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
        inspector: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Delivery is batched, so a failed send can no longer roll its own row back.

        Revoking reaches the same end state — no live invitation, no usable token — and,
        because the unique index is partial on exactly that, it is also what lets the
        administrator retry the address.
        """
        await register_owner(client, mailbox)
        principal_row = (
            await inspector.execute(text("SELECT id, org_id FROM users LIMIT 1"))
        ).one()

        from jutsu_api.security import Principal
        from jutsu_core.rbac import ROLE_PERMISSIONS

        actor = Principal(
            session_id=principal_row.id,
            identity_id=principal_row.id,
            user_id=principal_row.id,
            org_id=principal_row.org_id,
            role=Role.OWNER,
            permissions=ROLE_PERMISSIONS[Role.OWNER],
        )
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(principal_row.org_id)},
        )

        class RefusingOne:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send(self, message: EmailMessage) -> None:
                if message.to == "bounces@example.com":
                    raise RuntimeError("the provider refused it")
                self.sent.append(message.to)

        sender = RefusingOne()
        caplog.set_level("WARNING", logger="jutsu.invitations")
        result = await invite_many(
            db_session,
            actor=actor,
            rows=[BulkRow(email="bounces@example.com"), BulkRow(email="fine@example.com")],
            settings=settings,
            sender=sender,
        )

        assert result.sent == 1
        assert result.failed == 1
        assert sender.sent == ["fine@example.com"]
        await db_session.commit()
        live = (
            await inspector.execute(text("SELECT email FROM invitations WHERE revoked_at IS NULL"))
        ).scalars()
        assert list(live) == ["fine@example.com"]
        # §4.9: a delivery failure is logged as an event, never as the address it could
        # not reach. A traceback here would export the customer's mailing list one failed
        # import at a time.
        assert "bounces@example.com" not in caplog.text

    async def test_it_never_holds_a_token_in_a_response_body(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        # The token exists in exactly two places: the message and the database hash.
        await register_owner(client, mailbox)
        before = len(mailbox.messages)

        response = await client.post(
            "/v1/employees/invitations/bulk",
            json={"rows": [{"email": "one@example.com", "role": "member"}]},
            headers=csrf(client),
        )

        token = mailbox.messages[before].secrets["token"]
        assert token not in response.text


# --------------------------------------------------------------------------- authorization


class TestNothingHereWidensWhatSomebodyMayDo:
    async def test_a_member_cannot_reach_either_route(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Frontend hiding is not authorization, so the refusal has to be the server's."""
        await register_owner(client, mailbox)
        invited = await client.post(
            "/v1/employees/invitations",
            json={"email": "member@example.com", "role": "member"},
            headers=csrf(client),
        )
        assert invited.status_code == 202, invited.text
        # Accepting signs the client in as the invitee, who holds no member:invite.
        accepted = await client.post(
            "/v1/invitations/accept",
            json={"token": mailbox.last.secrets["token"], "full_name": "Charles Babbage"},
        )
        assert accepted.status_code == 200, accepted.text

        preview_response = await client.post(
            "/v1/employees/invitations/preview",
            json={"emails": "someone@example.com"},
            headers=csrf(client),
        )
        bulk_response = await client.post(
            "/v1/employees/invitations/bulk",
            json={"rows": [{"email": "someone@example.com", "role": "member"}]},
            headers=csrf(client),
        )

        assert preview_response.status_code == 403, preview_response.text
        assert bulk_response.status_code == 403, bulk_response.text

    async def test_neither_route_answers_without_a_session(self, client: AsyncClient) -> None:
        preview_response = await client.post(
            "/v1/employees/invitations/preview", json={"emails": "a@x.com"}
        )
        bulk_response = await client.post(
            "/v1/employees/invitations/bulk",
            json={"rows": [{"email": "a@x.com", "role": "member"}]},
        )

        assert preview_response.status_code == 401
        assert bulk_response.status_code == 401

    async def test_the_rank_ceiling_is_re_checked_on_the_send_not_only_the_preview(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        """A preview is advice; the send is the decision.

        The rows come back from the browser, so a client that skipped the preview — or
        edited its result — must meet the same ceiling. This is the case where believing
        the preview would be an escalation with a CSV attached.
        """
        await register_owner(client, mailbox)
        before = len(mailbox.messages)

        result = await send_bulk(client, [{"email": "boss@example.com", "role": "owner"}])

        assert outcomes(result) == {"boss@example.com": BulkOutcome.ROLE_TOO_HIGH}
        assert len(mailbox.messages) == before
        assert (await inspector.execute(text("SELECT count(*) FROM invitations"))).scalar_one() == 0

    async def test_an_invitation_a_bulk_send_created_still_writes_its_audit_row(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        # Reusing `invite_employee` is what guarantees this. A second insert path would
        # be a second place for the audit row to be forgotten.
        await register_owner(client, mailbox)

        await send_bulk(client, [{"email": "one@example.com", "role": "member"}])

        actions = (
            await inspector.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'member.invited'")
            )
        ).scalar_one()
        assert actions == 1


class TestTheParserCannotBeMadeToHang:
    """Bounds on adversarial input, which is what a paste box and an upload are."""

    def test_a_long_paste_with_no_bracket_is_linear(self) -> None:
        """The regression that matters: `_ANGLED` used to backtrack quadratically.

        Its display-name alternative could match the empty string, matched greedily, and
        overlapped the `\s*` beside it — so a paste with no `<` in it made the engine
        retry every split point at every start position. `BulkSource` accepts 64 000
        characters, and this runs on the request thread.
        """
        import time

        # The shape that was pathological: many words, spaces and no angle bracket.
        hostile = ("word " * 12_000)[:64_000]

        started = time.perf_counter()
        parse_pasted(hostile)
        elapsed = time.perf_counter() - started

        # Linear parsing does this in milliseconds. The old pattern did not finish.
        assert elapsed < 2.0, f"parse_pasted took {elapsed:.1f}s on a 64k paste"

    def test_a_bare_address_beside_a_contact_survives(self) -> None:
        # The old display-name alternative excluded quotes and commas but NOT spaces or
        # `@`, so it swallowed every plain address preceding a bracketed one on the same
        # line — silently dropping that person from the import.
        rows = parse_pasted("ada@example.com Grace Hopper <grace@example.com>")

        assert [row.email for row in rows] == ["ada@example.com", "grace@example.com"]

    def test_a_workbook_that_declares_a_gigabyte_is_refused(self) -> None:
        """`MAX_XLSX_BYTES` bounds the COMPRESSED archive, which is not a bound on work.

        XML compresses about a thousand to one, and `load_workbook` reads the whole
        shared-string table into a Python list before `read_only=True` defers anything —
        so `_MAX_SHEET_ROWS` never gets the chance to apply.
        """
        import io as _io
        import zipfile

        from jutsu_core.errors import ValidationFailed

        bomb = _io.BytesIO()
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/sharedStrings.xml", b"\0" * 60_000_000)

        payload = bomb.getvalue()
        assert len(payload) < MAX_XLSX_BYTES, "the point is that it is SMALL on the wire"

        with pytest.raises(ValidationFailed):
            parse_xlsx(payload)

    def test_a_csv_field_past_the_reader_limit_is_a_refusal_not_a_crash(self) -> None:
        # `csv.field_size_limit()` is 131 072 by default and `csv.reader` raises
        # mid-iteration — outside the `except` that only ever covered the dialect sniff,
        # so this answered 500 with a traceback for what is simply a bad file.
        from jutsu_core.errors import ValidationFailed

        with pytest.raises(ValidationFailed):
            parse_csv("email\n" + '"' + ("x" * 200_000) + '"' + "\n")

    def test_a_headerless_file_keeps_the_chosen_role(self) -> None:
        # It fell back to the dataclass default, silently seating a whole import as
        # Member whatever the administrator picked — unlike every other parse path.
        rows = parse_csv("a@x.com\nb@x.com\n", default_role=Role.ANALYST)

        assert [row.role for row in rows] == [Role.ANALYST, Role.ANALYST]

    def test_a_row_with_content_but_no_address_is_shown_rather_than_dropped(self) -> None:
        # Dropping it made the preview's total disagree with the file the person was
        # looking at, with nothing to say which line had gone.
        rows = parse_csv("email,role\n,member\nreal@example.com,member\n")

        assert [row.email for row in rows] == ["member", "real@example.com"]
