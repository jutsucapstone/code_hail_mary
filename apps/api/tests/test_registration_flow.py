"""The P1 vertical, end to end, against a real database.

Acceptance sentence: a person registers an organisation, receives a real message,
completes the OTP, and ends up with a session scoped to their own tenant and no other.

Every step here runs as the restricted application role. That is not incidental — the
`auth` schema tests only mean something because `jutsu_app` genuinely cannot read those
tables, and the tenancy tests only mean something because it genuinely cannot bypass
row-level security.
"""

from __future__ import annotations

import uuid

import pytest
from jutsu_api.auth_service import (
    ChallengePurpose,
    issue_challenge,
    open_session,
    resolve_principal,
    revoke_session,
    verify_challenge,
)
from jutsu_api.config import OTP_MAX_ATTEMPTS, Settings
from jutsu_api.email import RecordingEmailSender
from jutsu_api.registration import (
    JutsuIdAllocationExhausted,
    RegistrationRequest,
    register_organisation,
)
from jutsu_core.errors import Unauthenticated
from jutsu_core.ids import is_valid_jutsu_id
from jutsu_core.rbac import Permission, Role
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _request(email: str = "ada@example.com", domain: str = "example.com") -> RegistrationRequest:
    return RegistrationRequest(
        full_name="Ada Lovelace",
        work_email=email,
        company_name="Example Analytical",
        company_domain=domain,
        job_title="Head of Engineering",
        org_size="51-200",
    )


class TestRegistration:
    async def test_creates_the_whole_tenant_atomically(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        outcome = await register_organisation(
            db_session, _request(), settings=settings, sender=mailbox
        )
        await db_session.commit()

        assert outcome.created
        assert outcome.org_id is not None
        assert outcome.jutsu_id is not None
        assert is_valid_jutsu_id(outcome.jutsu_id)
        assert outcome.jutsu_id.startswith("JUTSU-ADM-"), "the first admin is an ADM, not an EMP"

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"),
            {"o": str(outcome.org_id)},
        )

        org_name = (
            await db_session.execute(
                text("SELECT name FROM orgs WHERE id = :i"), {"i": outcome.org_id}
            )
        ).scalar_one()
        assert org_name == "Example Analytical"

        role_key = (
            await db_session.execute(
                text("SELECT role_key FROM user_roles WHERE user_id = :u"), {"u": outcome.user_id}
            )
        ).scalar_one()
        assert role_key == Role.OWNER.value, "the first administrator must own the organisation"

        audited = (await db_session.execute(text("SELECT action, actor_id FROM audit_log"))).all()
        assert [action for action, _ in audited] == ["org.created"]
        assert all(actor == str(outcome.user_id) for _, actor in audited), (
            "the audit actor must be the opaque user id, never an email or a name"
        )

    async def test_the_jutsu_id_is_bound_to_the_user_in_the_ledger(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        outcome = await register_organisation(
            db_session, _request(), settings=settings, sender=mailbox
        )
        await db_session.commit()

        resolved = (
            await db_session.execute(
                text("SELECT org_id, user_id FROM auth.resolve_jutsu_id(:j)"),
                {"j": outcome.jutsu_id},
            )
        ).first()
        assert resolved is not None
        assert resolved.org_id == outcome.org_id
        assert resolved.user_id == outcome.user_id

    async def test_the_admin_has_no_acl_principal(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """The invariant `external_id` was made nullable for.

        A pilot administrator has no IdP subject, so they match no `document_acl` grant
        and see no evidence at all — regardless of holding the Owner role. Roles gate
        features; ACLs gate data.
        """
        outcome = await register_organisation(
            db_session, _request(), settings=settings, sender=mailbox
        )
        await db_session.commit()
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"),
            {"o": str(outcome.org_id)},
        )

        external_id = (
            await db_session.execute(
                text("SELECT external_id FROM users WHERE id = :u"), {"u": outcome.user_id}
            )
        ).scalar_one()
        assert external_id is None

    async def test_a_duplicate_domain_is_invisible_to_the_caller(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """Customer enumeration is the thing being prevented here.

        The second attempt must be indistinguishable over HTTP from the first. Only the
        email differs, and only the person controlling that address can read it.
        """
        first = await register_organisation(
            db_session, _request(), settings=settings, sender=mailbox
        )
        await db_session.commit()
        assert first.created

        second = await register_organisation(
            db_session,
            _request(email="grace@example.com"),
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()

        assert not second.created
        assert second.org_id is None

        # One organisation, not two.
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(first.org_id)}
        )
        count = (
            await db_session.execute(
                text("SELECT count(*) FROM orgs WHERE lower(domain) = 'example.com'")
            )
        ).scalar_one()
        assert count == 1

        # Both attempts sent mail, so timing and observable behaviour match.
        assert len(mailbox.messages) == 2

    async def test_allocation_gives_up_loudly_rather_than_looping(
        self,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exhaustion is an incident, not a retry-forever.

        Five consecutive collisions has probability ~6e-21 at the design ceiling, so if
        it happens the randomness is broken or something else is writing the ledger.
        Falling back to a longer or sequential id would turn a loud failure into a silent
        permanent weakening.
        """
        monkeypatch.setattr(
            "jutsu_api.registration.generate_jutsu_id", lambda _kind: "JUTSU-ADM-ZZZZZZZZ"
        )

        first = await register_organisation(
            db_session, _request(), settings=settings, sender=mailbox
        )
        await db_session.commit()
        assert first.created

        with pytest.raises(JutsuIdAllocationExhausted):
            await register_organisation(
                db_session,
                _request(email="b@other.example", domain="other.example"),
                settings=settings,
                sender=mailbox,
            )


class TestSignIn:
    async def _register(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> tuple[uuid.UUID, uuid.UUID]:
        outcome = await register_organisation(
            db_session, _request(), settings=settings, sender=mailbox
        )
        await db_session.commit()
        assert outcome.org_id is not None and outcome.user_id is not None
        return outcome.org_id, outcome.user_id

    async def test_the_full_path_from_email_to_scoped_session(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        org_id, user_id = await self._register(db_session, settings, mailbox)

        issued = await issue_challenge(
            db_session,
            address="ada@example.com",
            purpose=ChallengePurpose.SIGN_IN,
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()

        # Read the code out of the delivered message, exactly as a person would, rather
        # than from the database — which stores only a hash and could not be reversed.
        delivered = mailbox.last.secrets
        assert delivered["code"] == issued.code

        identity_id = await verify_challenge(db_session, token=issued.token, code=delivered["code"])
        credentials = await open_session(
            db_session, identity_id=identity_id, user_id=user_id, org_id=org_id
        )
        await db_session.commit()

        principal = await resolve_principal(db_session, token=credentials.token)
        assert principal.org_id == org_id
        assert principal.user_id == user_id
        assert principal.role is Role.OWNER
        assert principal.can(Permission.MEMBER_INVITE)

    async def test_a_challenge_cannot_be_redeemed_twice(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """Single use, enforced by a conditional UPDATE rather than a read-then-write."""
        await self._register(db_session, settings, mailbox)
        issued = await issue_challenge(
            db_session,
            address="ada@example.com",
            purpose=ChallengePurpose.SIGN_IN,
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()

        await verify_challenge(db_session, token=issued.token, code=issued.code)
        await db_session.commit()

        with pytest.raises(Unauthenticated):
            await verify_challenge(db_session, token=issued.token, code=issued.code)

    async def test_wrong_codes_spend_the_budget_and_then_lock_out(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """The attempt budget is transactional, so it cannot be spent in parallel.

        `CHECK (attempts <= max_attempts)` would not do this: under READ COMMITTED, N
        concurrent verifications all read attempts = 0 and the counter lands at 1,
        collapsing a 10^6 space to ~10^3 rounds. The increment and the filter are one
        statement precisely so that cannot happen.
        """
        await self._register(db_session, settings, mailbox)
        issued = await issue_challenge(
            db_session,
            address="ada@example.com",
            purpose=ChallengePurpose.SIGN_IN,
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()

        for _ in range(OTP_MAX_ATTEMPTS):
            with pytest.raises(Unauthenticated):
                await verify_challenge(db_session, token=issued.token, code="000000")
            await db_session.commit()

        # Budget exhausted: even the correct code is now refused, and the refusal is
        # worded identically so it cannot be used to confirm the code was right.
        with pytest.raises(Unauthenticated):
            await verify_challenge(db_session, token=issued.token, code=issued.code)

    async def test_an_unknown_address_does_the_same_work(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """No account-existence oracle.

        A challenge is written and a message is sent for an address with no account, so
        the observable behaviour — including the time taken — matches. The message says
        there is no account; nothing over HTTP does.
        """
        issued = await issue_challenge(
            db_session,
            address="nobody@nowhere.example",
            purpose=ChallengePurpose.SIGN_IN,
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()

        assert issued.challenge_id is not None
        assert len(mailbox.messages) == 1
        # The code is withheld from a message to an address with no account, so an
        # attacker who can read that inbox still cannot obtain a usable credential.
        assert mailbox.last.secrets == {}

    async def test_signing_out_revokes_the_session(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        org_id, user_id = await self._register(db_session, settings, mailbox)
        issued = await issue_challenge(
            db_session,
            address="ada@example.com",
            purpose=ChallengePurpose.SIGN_IN,
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()
        identity_id = await verify_challenge(db_session, token=issued.token, code=issued.code)
        credentials = await open_session(
            db_session, identity_id=identity_id, user_id=user_id, org_id=org_id
        )
        await db_session.commit()

        assert await revoke_session(db_session, token=credentials.token)
        await db_session.commit()

        with pytest.raises(Unauthenticated):
            await resolve_principal(db_session, token=credentials.token)

        # Idempotent: signing out twice is not an error.
        assert not await revoke_session(db_session, token=credentials.token)

    async def test_an_unknown_session_token_is_refused(self, db_session: AsyncSession) -> None:
        with pytest.raises(Unauthenticated):
            await resolve_principal(db_session, token="not-a-real-session-token")
