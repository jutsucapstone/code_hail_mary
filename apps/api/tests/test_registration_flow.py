"""The P1 vertical, end to end, against a real database.

Acceptance sentence: a person stages an organisation, receives a real message, completes
the OTP, and only then does the tenant exist — scoped to them and to nobody else.

Every step here runs as the restricted application role. That is not incidental — the
`auth` schema tests only mean something because `jutsu_app` genuinely cannot read those
tables, and the tenancy tests only mean something because it genuinely cannot bypass
row-level security.

**The ordering assertions are the point of this file.** Registration used to create the
tenant and mail afterwards, which let an unauthenticated caller claim any domain and read
the confirmation in their own inbox. Most of what follows exists to prove that the write
now happens after the proof, and keeps happening after it.
"""

from __future__ import annotations

import uuid

import pytest
from jutsu_api.auth_service import (
    ChallengePurpose,
    email_hmac,
    issue_challenge,
    open_session,
    resolve_principal,
    revoke_session,
    verify_challenge,
)
from jutsu_api.config import (
    OTP_MAX_ATTEMPTS,
    REGISTRATION_BUDGET_LIMIT,
    TERMS_DOCUMENTS,
    Settings,
)
from jutsu_api.email import RecordingEmailSender
from jutsu_api.registration import (
    DomainAlreadyRegistered,
    InvalidDomain,
    JutsuIdAllocationExhausted,
    NoPendingRegistration,
    RegistrationRequest,
    TooManyRegistrations,
    complete_registration,
    stage_registration,
)
from jutsu_core.errors import Unauthenticated
from jutsu_core.ids import is_valid_jutsu_id
from jutsu_core.rbac import Permission, Role
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _request(
    email: str = "ada@example.com",
    domain: str = "example.com",
    **overrides: object,
) -> RegistrationRequest:
    fields: dict[str, object] = {
        "full_name": "Ada Lovelace",
        "work_email": email,
        "company_name": "Example Analytical",
        "company_domain": domain,
        "job_title": "Head of Engineering",
        "org_size": "51-200",
        "country": "GB",
        "industry": "technology",
    }
    fields.update(overrides)
    return RegistrationRequest(**fields)  # type: ignore[arg-type]


async def _count_orgs(inspector: AsyncSession, domain: str) -> int:
    """Counts through the privileged connection, never through the app role.

    `orgs` is under FORCE row-level security and the app role sets its tenant scope
    per transaction, so an unscoped count there returns 0 for every domain — including
    ones that exist. An assertion built on that is vacuous in the one direction that
    matters here: "nothing was created yet" would pass even if everything had been.
    """
    count = (
        await inspector.execute(
            text("SELECT count(*) FROM orgs WHERE lower(domain) = :d"), {"d": domain}
        )
    ).scalar_one()
    return int(count)


async def _register(
    session: AsyncSession,
    settings: Settings,
    mailbox: RecordingEmailSender,
    request: RegistrationRequest | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Stage, redeem, complete — the whole happy path. Returns (org, user, jutsu_id)."""
    pending = await stage_registration(
        session, request or _request(), settings=settings, sender=mailbox
    )
    await session.commit()

    redeemed = await verify_challenge(
        session,
        token=pending.token,
        code=mailbox.last.secrets["code"],
        expected_purpose=ChallengePurpose.REGISTER,
    )
    outcome = await complete_registration(
        session,
        token=pending.token,
        identity_id=redeemed.identity_id,
        challenge_id=redeemed.challenge_id,
        settings=settings,
    )
    await session.commit()
    assert outcome.org_id is not None and outcome.user_id is not None
    assert outcome.jutsu_id is not None
    return outcome.org_id, outcome.user_id, outcome.jutsu_id


class TestStaging:
    """What happens before the code comes back — which must be nothing durable."""

    async def test_staging_creates_no_organisation_and_no_user(
        self,
        inspector: AsyncSession,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
    ) -> None:
        """The whole reason this redesign exists.

        Before it, this call created the org, the owner, the role and the JUTSU ID, so
        `company_domain` was claimable by anyone who could type it.
        """
        await stage_registration(db_session, _request(), settings=settings, sender=mailbox)
        await db_session.commit()

        assert await _count_orgs(inspector, "example.com") == 0
        assert (await db_session.execute(text("SELECT count(*) FROM users"))).scalar_one() == 0
        assert (await db_session.execute(text("SELECT count(*) FROM user_roles"))).scalar_one() == 0
        # And the message went out, so the observable behaviour is unchanged.
        assert len(mailbox.messages) == 1

    async def test_an_address_off_the_domain_is_accepted_and_mailed(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """The code goes to whatever address was given.

        This used to raise. Requiring the work email to sit on the organisation's domain
        turned away every founder whose company has no mail on its own domain yet, and
        every pilot run from a personal address. What the relaxation costs is asserted
        below, in `test_an_unproven_domain_is_recorded_as_unverified` — the organisation
        is created, but its domain is not marked as proven by anybody.
        """
        pending = await stage_registration(
            db_session,
            _request(email="founder@gmail.com", domain="acme.com"),
            settings=settings,
            sender=mailbox,
        )

        assert pending.token
        assert len(mailbox.messages) == 1
        assert mailbox.last.to == "founder@gmail.com"

    async def test_a_subdomain_is_accepted_and_still_not_proof(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """A mailbox on a subdomain no longer blocks registration, and never proved the
        apex. The distinction now lives in `domain_verified_at` rather than in a refusal."""
        pending = await stage_registration(
            db_session,
            _request(email="ada@mail.example.com", domain="example.com"),
            settings=settings,
            sender=mailbox,
        )
        assert pending.token

    @pytest.mark.parametrize("bad", ["acme", "not a domain", "@@@", "a..b"])
    async def test_an_unreadable_domain_is_a_422_and_not_a_crash(
        self,
        bad: str,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
    ) -> None:
        """`DomainError` is not a `JutsuError`, so it used to escape staging entirely and
        land in the catch-all handler — a domain typed without a dot returned a 500.
        Confirmed against production before this was fixed."""
        with pytest.raises(InvalidDomain):
            await stage_registration(
                db_session,
                _request(email="ada@example.com", domain=bad),
                settings=settings,
                sender=mailbox,
            )

        assert mailbox.messages == []

    async def test_a_pasted_address_reduces_to_its_domain(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """`er.ritikraj27@gmail.com` in the domain box parses as three dot-separated
        labels, so it was accepted verbatim — the organisation then claimed a domain
        containing an @, and the form told the registrant to use an address at it."""
        pending = await stage_registration(
            db_session,
            _request(email="er.ritikraj27@gmail.com", domain="er.ritikraj27@gmail.com"),
            settings=settings,
            sender=mailbox,
        )
        assert pending.token

    async def test_case_and_trailing_dot_do_not_defeat_the_match(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """`Ada@Example.COM` against `example.com.` is one authority, not two."""
        pending = await stage_registration(
            db_session,
            _request(email="Ada@Example.COM", domain="example.com."),
            settings=settings,
            sender=mailbox,
        )
        assert pending.token

    async def test_the_budget_refuses_a_flood(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """Staging mails an address the caller names, so it needs a ceiling.

        Without one it is an open relay that also writes an identity row and a staged
        payload per request.
        """
        for _ in range(REGISTRATION_BUDGET_LIMIT):
            await stage_registration(db_session, _request(), settings=settings, sender=mailbox)
        await db_session.commit()

        with pytest.raises(TooManyRegistrations):
            await stage_registration(db_session, _request(), settings=settings, sender=mailbox)

        assert len(mailbox.messages) == REGISTRATION_BUDGET_LIMIT

    async def test_staging_says_the_same_thing_for_a_taken_domain(
        self,
        inspector: AsyncSession,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
    ) -> None:
        """No enumeration oracle, and no lock either.

        A second person staging a domain that already exists gets the ordinary staged
        result — the duplicate is only discovered after they prove a mailbox at it. A
        constraint here would answer the question without any mailbox at all, and would
        additionally let a stranger hold the domain for the row's lifetime.
        """
        await _register(db_session, settings, mailbox)

        pending = await stage_registration(
            db_session,
            _request(email="grace@example.com"),
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()
        assert pending.token
        assert await _count_orgs(inspector, "example.com") == 1


class TestCompletion:
    async def test_an_unproven_domain_is_recorded_as_unverified(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """The price of accepting any address, written down where it can be read.

        Registering `acme.com` from a gmail address now succeeds — but nobody proved
        anything about `acme.com`, so the column that says so stays NULL. Anything that
        later grants authority from a domain must read this and not `orgs.domain`; the
        assertion exists so that stays true.
        """
        org_id, _user_id, _jid = await _register(
            db_session,
            settings,
            mailbox,
            _request(email="founder@gmail.com", domain="acme.com"),
        )

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
        )
        row = (
            await db_session.execute(
                text("SELECT domain, domain_verified_at FROM orgs WHERE id = :id"),
                {"id": org_id},
            )
        ).one()

        assert row.domain == "acme.com", "the organisation is created either way"
        assert row.domain_verified_at is None, (
            "a gmail address proves gmail.com and nothing about acme.com"
        )

    async def test_creates_the_whole_tenant_atomically(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        org_id, user_id, jutsu_id = await _register(db_session, settings, mailbox)

        assert is_valid_jutsu_id(jutsu_id)
        assert jutsu_id.startswith("JUTSU-ADM-"), "the first admin is an ADM, not an EMP"

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
        )

        row = (
            await db_session.execute(
                text(
                    "SELECT name, domain, country, industry, domain_verified_at "
                    "FROM orgs WHERE id = :id"
                ),
                {"id": org_id},
            )
        ).one()
        assert row.name == "Example Analytical"
        assert row.domain == "example.com"
        assert row.country == "GB"
        assert row.industry == "technology"
        # Proved, not merely claimed — and the column exists so the difference is
        # expressible for every row that came before this migration.
        assert row.domain_verified_at is not None

        role = (
            await db_session.execute(
                text("SELECT role_key FROM user_roles WHERE user_id = :u"), {"u": user_id}
            )
        ).scalar_one()
        assert role == Role.OWNER.value

    async def test_exactly_one_organisation_and_one_owner(
        self,
        inspector: AsyncSession,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
    ) -> None:
        org_id, _user_id, _jid = await _register(db_session, settings, mailbox)

        assert await _count_orgs(inspector, "example.com") == 1
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
        )
        owners = (
            await db_session.execute(
                text("SELECT count(*) FROM user_roles WHERE role_key = :r"),
                {"r": Role.OWNER.value},
            )
        ).scalar_one()
        assert owners == 1

    async def test_terms_acceptance_is_recorded_with_version_and_timestamp(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """Both documents, versioned from server constants, with consent and record
        timestamps kept apart."""
        org_id, user_id, _jid = await _register(db_session, settings, mailbox)

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
        )
        rows = (
            await db_session.execute(
                text(
                    "SELECT document, version, accepted_at, recorded_at "
                    "FROM terms_acceptances WHERE user_id = :u ORDER BY document"
                ),
                {"u": user_id},
            )
        ).all()

        assert {r.document for r in rows} == set(TERMS_DOCUMENTS)
        for row in rows:
            assert row.version == TERMS_DOCUMENTS[row.document]
            # Consent precedes the record; conflating them would make the legal record
            # assert a moment the person was not at a keyboard.
            assert row.accepted_at <= row.recorded_at

    async def test_a_pending_registration_is_single_use(
        self,
        inspector: AsyncSession,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
    ) -> None:
        """A resend leaves two live challenges against one staged payload.

        Without compare-and-set consumption both redemptions would create an
        organisation, and the second would burn a JUTSU ID on a tenant nobody asked for.
        """
        pending = await stage_registration(
            db_session, _request(), settings=settings, sender=mailbox
        )
        await db_session.commit()

        redeemed = await verify_challenge(
            db_session,
            token=pending.token,
            code=mailbox.last.secrets["code"],
            expected_purpose=ChallengePurpose.REGISTER,
        )
        await complete_registration(
            db_session,
            token=pending.token,
            identity_id=redeemed.identity_id,
            challenge_id=redeemed.challenge_id,
            settings=settings,
        )
        await db_session.commit()

        # Replaying the same token, as a refresh or a back-button resubmit would.
        with pytest.raises(NoPendingRegistration):
            await complete_registration(
                db_session,
                token=pending.token,
                identity_id=redeemed.identity_id,
                challenge_id=redeemed.challenge_id,
                settings=settings,
            )
        assert await _count_orgs(inspector, "example.com") == 1

    async def test_a_second_registration_for_one_domain_is_refused_after_proof(
        self,
        inspector: AsyncSession,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
    ) -> None:
        """The concurrent-race resolution, played out sequentially.

        `uq_orgs_domain_active` is the single authority on who owns a domain, so the
        loser of the race is whoever commits second — and is told plainly, because they
        proved a mailbox at that exact domain and the answer they need is "ask for an
        invitation".
        """
        await _register(db_session, settings, mailbox)

        second = await stage_registration(
            db_session,
            _request(email="grace@example.com"),
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()
        redeemed = await verify_challenge(
            db_session,
            token=second.token,
            code=mailbox.last.secrets["code"],
            expected_purpose=ChallengePurpose.REGISTER,
        )

        with pytest.raises(DomainAlreadyRegistered):
            await complete_registration(
                db_session,
                token=second.token,
                identity_id=redeemed.identity_id,
                challenge_id=redeemed.challenge_id,
                settings=settings,
            )
        await db_session.rollback()
        assert await _count_orgs(inspector, "example.com") == 1

    async def test_the_jutsu_id_is_bound_to_the_user_in_the_ledger(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        org_id, user_id, jutsu_id = await _register(db_session, settings, mailbox)

        bound = (
            await db_session.execute(
                text("SELECT org_id, user_id FROM auth.resolve_jutsu_id(CAST(:j AS text))"),
                {"j": jutsu_id},
            )
        ).first()
        assert bound is not None
        assert uuid.UUID(str(bound.org_id)) == org_id
        assert uuid.UUID(str(bound.user_id)) == user_id

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
        await _register(db_session, settings, mailbox)

        monkeypatch.setattr(
            "jutsu_api.registration.generate_jutsu_id", lambda _kind: "JUTSU-ADM-ZZZZZZZZ"
        )
        first = await stage_registration(
            db_session,
            _request(email="ada@other.example", domain="other.example"),
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()
        redeemed = await verify_challenge(
            db_session,
            token=first.token,
            code=mailbox.last.secrets["code"],
            expected_purpose=ChallengePurpose.REGISTER,
        )
        await complete_registration(
            db_session,
            token=first.token,
            identity_id=redeemed.identity_id,
            challenge_id=redeemed.challenge_id,
            settings=settings,
        )
        await db_session.commit()

        second = await stage_registration(
            db_session,
            _request(email="ada@third.example", domain="third.example"),
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()
        redeemed2 = await verify_challenge(
            db_session,
            token=second.token,
            code=mailbox.last.secrets["code"],
            expected_purpose=ChallengePurpose.REGISTER,
        )
        with pytest.raises(JutsuIdAllocationExhausted):
            await complete_registration(
                db_session,
                token=second.token,
                identity_id=redeemed2.identity_id,
                challenge_id=redeemed2.challenge_id,
                settings=settings,
            )


class TestChallengePurpose:
    async def test_a_sign_in_code_cannot_complete_a_registration(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """Purpose confusion, closed.

        `auth.consume_attempt` has returned `purpose` since migration 0003 and nothing
        read it until now. Without the check, a victim's ordinary sign-in code would
        complete an attacker's staged registration.
        """
        issued = await issue_challenge(
            db_session,
            address="ada@example.com",
            purpose=ChallengePurpose.SIGN_IN,
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()

        with pytest.raises(Unauthenticated):
            await verify_challenge(
                db_session,
                token=issued.token,
                code=issued.code,
                expected_purpose=ChallengePurpose.REGISTER,
            )

    async def test_a_registration_code_cannot_open_a_session(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        pending = await stage_registration(
            db_session, _request(), settings=settings, sender=mailbox
        )
        await db_session.commit()

        with pytest.raises(Unauthenticated):
            await verify_challenge(
                db_session,
                token=pending.token,
                code=mailbox.last.secrets["code"],
                expected_purpose=ChallengePurpose.SIGN_IN,
            )

    async def test_a_wrong_purpose_still_spends_the_attempt(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """A mismatched purpose must not be a free probe of the code space."""
        pending = await stage_registration(
            db_session, _request(), settings=settings, sender=mailbox
        )
        await db_session.commit()

        for _ in range(OTP_MAX_ATTEMPTS):
            with pytest.raises(Unauthenticated):
                await verify_challenge(
                    db_session,
                    token=pending.token,
                    code=mailbox.last.secrets["code"],
                    expected_purpose=ChallengePurpose.SIGN_IN,
                )

        # Budget gone, so even the right purpose with the right code is now refused.
        with pytest.raises(Unauthenticated):
            await verify_challenge(
                db_session,
                token=pending.token,
                code=mailbox.last.secrets["code"],
                expected_purpose=ChallengePurpose.REGISTER,
            )


class TestAttemptBudget:
    async def test_a_failed_attempt_survives_the_rollback_that_follows_it(
        self,
        inspector: AsyncSession,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
    ) -> None:
        """The regression that made six digits guessable without limit.

        `get_db` wraps a request in one transaction, and `verify_challenge` raised after
        spending an attempt — so the spend rolled back with everything else and the
        budget never depleted. The previous version of this test hid it by committing by
        hand between attempts, which production does not do.

        No commit here on purpose. That is the whole assertion.
        """
        issued = await issue_challenge(
            db_session,
            address="ada@example.com",
            purpose=ChallengePurpose.SIGN_IN,
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()

        with pytest.raises(Unauthenticated):
            await verify_challenge(
                db_session,
                token=issued.token,
                code="000000",
                expected_purpose=ChallengePurpose.SIGN_IN,
            )
        await db_session.rollback()

        # Read through the owner: `jutsu_app` has no grant on auth tables at all,
        # which is the boundary these tests exist to keep.
        spent = (
            await inspector.execute(
                text("SELECT attempts FROM auth.login_challenges WHERE id = :c"),
                {"c": issued.challenge_id},
            )
        ).scalar_one()
        assert spent == 1, "the attempt was rolled back; the budget is a decoration"

    async def test_wrong_codes_spend_the_budget_and_then_lock_out(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """The attempt budget is transactional, so it cannot be spent in parallel."""
        await _register(db_session, settings, mailbox)
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
                await verify_challenge(
                    db_session,
                    token=issued.token,
                    code="000000",
                    expected_purpose=ChallengePurpose.SIGN_IN,
                )

        # Budget exhausted: even the correct code is now refused, and the refusal is
        # worded identically so it cannot be used to confirm the code was right.
        with pytest.raises(Unauthenticated):
            await verify_challenge(
                db_session,
                token=issued.token,
                code=issued.code,
                expected_purpose=ChallengePurpose.SIGN_IN,
            )


class TestExpiryAndReplay:
    async def test_an_expired_pending_registration_creates_nothing(
        self,
        inspector: AsyncSession,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
    ) -> None:
        pending = await stage_registration(
            db_session, _request(), settings=settings, sender=mailbox
        )
        await db_session.commit()

        redeemed = await verify_challenge(
            db_session,
            token=pending.token,
            code=mailbox.last.secrets["code"],
            expected_purpose=ChallengePurpose.REGISTER,
        )
        # Age the staged row past its window. The challenge is already redeemed, so this
        # isolates the staging expiry rather than the challenge's.
        await inspector.execute(
            text(
                "UPDATE auth.pending_registrations SET expires_at = now() - interval '1 second' "
                "WHERE challenge_id = :c"
            ),
            {"c": redeemed.challenge_id},
        )
        await inspector.commit()

        with pytest.raises(NoPendingRegistration):
            await complete_registration(
                db_session,
                token=pending.token,
                identity_id=redeemed.identity_id,
                challenge_id=redeemed.challenge_id,
                settings=settings,
            )
        assert await _count_orgs(inspector, "example.com") == 0

    async def test_an_expired_challenge_is_refused(
        self,
        inspector: AsyncSession,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
    ) -> None:
        pending = await stage_registration(
            db_session, _request(), settings=settings, sender=mailbox
        )
        await db_session.commit()
        await inspector.execute(
            text(
                "UPDATE auth.login_challenges SET expires_at = now() - interval '1 second' "
                "WHERE id = :c"
            ),
            {"c": pending.challenge_id},
        )
        await inspector.commit()

        with pytest.raises(Unauthenticated):
            await verify_challenge(
                db_session,
                token=pending.token,
                code=mailbox.last.secrets["code"],
                expected_purpose=ChallengePurpose.REGISTER,
            )
        assert await _count_orgs(inspector, "example.com") == 0

    async def test_the_reaper_removes_abandoned_rows(
        self,
        inspector: AsyncSession,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
    ) -> None:
        """An expiry column with nothing deleting it is a comment, not a control."""
        await stage_registration(db_session, _request(), settings=settings, sender=mailbox)
        await db_session.commit()
        await inspector.execute(
            text("UPDATE auth.pending_registrations SET expires_at = now() - interval '1 hour'")
        )
        await inspector.commit()

        removed = (
            await db_session.execute(text("SELECT auth.reap_expired_registrations()"))
        ).scalar_one()
        await db_session.commit()
        assert removed >= 1


class TestAntiEnumeration:
    async def test_staging_does_the_same_work_for_a_free_and_a_taken_domain(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """Both stage, both mail, both return a token. No branch to observe."""
        free = await stage_registration(
            db_session,
            _request(email="ada@free.example", domain="free.example"),
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()
        messages_after_free = len(mailbox.messages)

        await _register(db_session, settings, mailbox)
        taken = await stage_registration(
            db_session,
            _request(email="grace@example.com"),
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()

        assert free.token and taken.token
        assert messages_after_free == 1
        # One message per staging call regardless of whether the domain was taken.
        assert len(mailbox.messages) > messages_after_free

    async def test_the_staged_payload_is_unreachable_without_the_token(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """Possession of the emailed token is what unlocks the payload.

        Knowing the address is not enough — which is what stops a stranger's staging POST
        from overwriting or reading a victim's pending row.
        """
        await stage_registration(db_session, _request(), settings=settings, sender=mailbox)
        await db_session.commit()

        wrong = (
            await db_session.execute(
                text(
                    "SELECT challenge_id FROM auth.consume_pending_registration("
                    "  decode(repeat('00', 32), 'hex'))"
                )
            )
        ).first()
        assert wrong is None

    async def test_the_application_role_cannot_read_staged_registrations(
        self, db_session: AsyncSession
    ) -> None:
        """`auth` is protected by privilege, not by RLS.

        The staged payload holds a name, an address and a job title, so a direct read
        must be denied outright rather than merely filtered.
        """
        with pytest.raises(Exception, match="permission denied"):
            await db_session.execute(text("SELECT * FROM auth.pending_registrations"))
        await db_session.rollback()

    async def test_registration_events_record_the_hmac_never_the_address(
        self,
        inspector: AsyncSession,
        db_session: AsyncSession,
        settings: Settings,
        mailbox: RecordingEmailSender,
    ) -> None:
        """The pre-organisation audit trail, without PII (§4.9)."""
        await stage_registration(db_session, _request(), settings=settings, sender=mailbox)
        await db_session.commit()

        row = (
            await inspector.execute(
                text(
                    "SELECT email_hmac, domain, outcome FROM auth.registration_events "
                    "ORDER BY occurred_at DESC LIMIT 1"
                )
            )
        ).first()
        assert row is not None
        assert bytes(row.email_hmac) == email_hmac("ada@example.com", settings)
        assert row.domain == "example.com"
        assert row.outcome == "staged"


class TestSignIn:
    async def test_the_full_path_from_email_to_scoped_session(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        org_id, user_id, _jid = await _register(db_session, settings, mailbox)

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

        redeemed = await verify_challenge(
            db_session,
            token=issued.token,
            code=delivered["code"],
            expected_purpose=ChallengePurpose.SIGN_IN,
        )
        credentials = await open_session(
            db_session, identity_id=redeemed.identity_id, user_id=user_id, org_id=org_id
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
        await _register(db_session, settings, mailbox)
        issued = await issue_challenge(
            db_session,
            address="ada@example.com",
            purpose=ChallengePurpose.SIGN_IN,
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()

        await verify_challenge(
            db_session,
            token=issued.token,
            code=issued.code,
            expected_purpose=ChallengePurpose.SIGN_IN,
        )
        await db_session.commit()

        with pytest.raises(Unauthenticated):
            await verify_challenge(
                db_session,
                token=issued.token,
                code=issued.code,
                expected_purpose=ChallengePurpose.SIGN_IN,
            )

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
        assert mailbox.last.secrets == {}

    async def test_signing_out_revokes_the_session(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        org_id, user_id, _jid = await _register(db_session, settings, mailbox)

        # A real identity, not a fresh uuid: `sessions.identity_id` carries a foreign
        # key to `auth.identities`, so an invented one is rejected by the database
        # rather than quietly opening an orphan session.
        issued = await issue_challenge(
            db_session,
            address="ada@example.com",
            purpose=ChallengePurpose.SIGN_IN,
            settings=settings,
            sender=mailbox,
        )
        await db_session.commit()
        redeemed = await verify_challenge(
            db_session,
            token=issued.token,
            code=issued.code,
            expected_purpose=ChallengePurpose.SIGN_IN,
        )
        credentials = await open_session(
            db_session, identity_id=redeemed.identity_id, user_id=user_id, org_id=org_id
        )
        await db_session.commit()

        await revoke_session(db_session, token=credentials.token)
        await db_session.commit()

        with pytest.raises(Unauthenticated):
            await resolve_principal(db_session, token=credentials.token)

    async def test_an_unknown_session_token_is_refused(self, db_session: AsyncSession) -> None:
        with pytest.raises(Unauthenticated):
            await resolve_principal(db_session, token="not-a-real-session-token")
