"""Organisation registration, in two halves separated by proof of a mailbox.

**The work email does not have to be on the organisation's domain, and that is a
deliberate trade with a live consequence.** Requiring it turned away every founder whose
company has no mail on its own domain yet, and every pilot run from a personal address,
so the requirement was dropped. What it was also doing, as a side effect, was preventing
`eve@evil.example` from registering `microsoft.com`: proving a mailbox *at the claimed
domain* is what separated a claim from a proof. Nothing does that now. An anonymous
caller can register any unclaimed domain from any inbox they control, and because
`uq_orgs_domain_active` is unique, the real company is then turned away.

`domain_verified_at` is the honest record of the difference: a timestamp when the
verified address is on the domain, NULL when it is not. **Anything that grants authority
from a domain — auto-join, directory claims, support routing — must read that column and
never `orgs.domain`.** Nothing does today. Closing the squatting hole properly means
either verifying domains out of band (a DNS record, as Resend does) or refusing free-mail
providers; both are open.

**Nothing durable is created until the code comes back.** Staging writes one row in
`auth.pending_registrations` and sends a message; the organisation, its owner, that
person's role and their JUTSU ID all come into existence in a single transaction on the
verify side, and only if the redeemed challenge is the one the staged payload was bound
to. Before this split, registration created the whole tenant first and mailed afterwards,
so a domain could be claimed without reading any inbox at all. The split still buys that:
whoever registers must at least control the address they gave.

**The staged payload is reachable only with the emailed token.** The row is keyed on
`token_digest(token)`, the same value `auth.invitation_tokens` uses for the same reason.
Keying on the address or the identity instead would let a stranger's later staging POST
overwrite a victim's pending row, terms acceptance included.

**The scope is set before the organisation exists.** `orgs` carries a `WITH CHECK` policy
on `id`, so an unscoped INSERT is rejected — and a row cannot be scoped to itself before
it is written. The id is therefore minted here and the GUC set to it first. That ordering
looks odd until you try it the other way round, which is why the fixture in
`packages/db/tests/conftest.py` has the same shape.

**A duplicate domain never changes the HTTP response at staging.** Telling an anonymous
caller "that company is already registered" is a customer-enumeration oracle. At verify
it is still disclosed, because leaving someone at a generic failure means the real answer
("ask your administrator for an invitation") never reaches the person who needs it — and
whoever is reading has at least proved a mailbox and spent an attempt from the budget,
rather than typing a domain into a form. That is a weaker justification than it was when
the address had to be on the domain in question, and it is the second place the relaxed
check is felt.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from jutsu_core.domains import DomainError, canonical_domain, domain_of, normalise_email
from jutsu_core.errors import JutsuError
from jutsu_core.ids import JutsuIdKind, generate_jutsu_id
from jutsu_core.rbac import Role
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.auth_service import (
    ChallengePurpose,
    email_hmac,
    issue_challenge,
    token_digest,
)
from jutsu_api.config import (
    CHALLENGE_TTL_SECONDS,
    REGISTRATION_BUDGET_LIMIT,
    REGISTRATION_BUDGET_WINDOW_SECONDS,
    TERMS_DOCUMENTS,
    Settings,
)
from jutsu_api.email import EmailSender

__all__ = [
    "InvalidDomain",
    "PendingRegistration",
    "RegistrationOutcome",
    "RegistrationRequest",
    "TooManyRegistrations",
    "allocate_jutsu_id",
    "complete_registration",
    "stage_registration",
]

#: Attempts before allocation is treated as broken rather than unlucky.
#:
#: Derived, not chosen by feel: the space is 32**8, so at 10**8 allocated ids the
#: per-insert collision rate is 9.09e-5 and five consecutive collisions have probability
#: 6.22e-21. Exhausting this loop is therefore not bad luck — it means the CSPRNG is
#: degraded or the ledger is being written by something else, both of which are incidents
#: that must surface rather than be papered over with a longer id.
JUTSU_ID_ATTEMPTS = 5

#: The constraint that decides who owns a domain. Branching on the name rather than on
#: `IntegrityError` matters: a JUTSU ID collision inside the same transaction is also an
#: IntegrityError, and reporting it as "that domain is taken" would send someone away
#: from a domain that is in fact free.
DOMAIN_CONSTRAINT = "uq_orgs_domain_active"

#: Shown for a value that cannot be read as a domain. Names the shape wanted rather than
#: restating the rule, because "invalid domain" alone leaves the reader guessing which of
#: the two fields on the previous pane is wrong, and the usual mistake is pasting a whole
#: address into it.
_INVALID_DOMAIN_MESSAGE = (
    "That does not look like an organisation domain. Use the domain on its own, like acme.com."
)


class JutsuIdAllocationExhausted(JutsuError):
    status_code = 503
    code = "service_unavailable"


class InvalidDomain(JutsuError):
    """The organisation domain cannot be read as a domain at all.

    Was `DomainMismatch`, and meant something else: that the work address was not on the
    domain being claimed. That is no longer refused, so the name and the code would have
    described a rejection that can no longer happen while quietly continuing to fire for
    a malformed value — the sort of drift that makes an error code untrustworthy.

    A 422 rather than a 403: nothing is forbidden, one field is unreadable and the caller
    can fix it. Safe to state plainly — the value came from this same request, so it
    discloses nothing they did not type.
    """

    status_code = 422
    code = "invalid_domain"


class TooManyRegistrations(JutsuError):
    status_code = 429
    code = "rate_limited"


@dataclass(frozen=True, slots=True)
class RegistrationRequest:
    full_name: str
    work_email: str
    company_name: str
    company_domain: str
    job_title: str
    org_size: str
    #: Optional. Nothing reads them yet; they are collected because onboarding is the
    #: only moment anyone will answer, and timezone/residency work needs them later.
    country: str | None = None
    industry: str | None = None


@dataclass(frozen=True, slots=True)
class PendingRegistration:
    """A staged registration, before anyone has proved anything."""

    token: str
    challenge_id: UUID


@dataclass(frozen=True, slots=True)
class RegistrationOutcome:
    """What happened. Safe to return from the verify side, never from staging.

    The last three fields exist for the welcome email and nothing else. They are values
    this function already held — the staged payload and the row it just wrote — so
    carrying them out is cheaper than a second read, and it keeps the caller from having
    to re-query a tenant inside the same transaction that created it.

    None of it reaches the HTTP response. `register_verify` returns a destination and
    nothing more, for the same reason staging returns nothing: the next thing the
    registrant needs is in their inbox.
    """

    created: bool
    org_id: UUID | None
    user_id: UUID | None
    jutsu_id: str | None
    org_name: str | None = None
    org_domain: str | None = None
    #: The address that was verified, and therefore the only one this organisation's
    #: welcome may be sent to.
    owner_email: str | None = None
    #: What the registrant was actually made, read out of this function rather than
    #: assumed by the caller. The welcome email states it, and a second place asserting
    #: "the first administrator is an Owner" is a second place to update if that ever
    #: stops being true.
    owner_role: Role | None = None


async def allocate_jutsu_id(session: AsyncSession, *, org_id: UUID, kind: JutsuIdKind) -> str:
    """Reserve an unused id from the ledger.

    Shared by registration and by invitation acceptance — the two paths that bring a
    person into existence. Keeping one implementation means the retry bound, the typed
    exhaustion error and the ON CONFLICT semantics cannot diverge between them.

    `auth.reserve_jutsu_id` uses ON CONFLICT DO NOTHING, so a taken id returns NULL rather
    than raising. That distinction is what makes this loop safe inside the registration
    transaction: an IntegrityError would abort every statement after it and there would be
    no transaction left to retry in.
    """
    for _ in range(JUTSU_ID_ATTEMPTS):
        candidate = generate_jutsu_id(kind)
        reserved = (
            await session.execute(
                text("SELECT auth.reserve_jutsu_id(:jid, :org, :kind)"),
                {"jid": candidate, "org": org_id, "kind": kind.value},
            )
        ).scalar_one_or_none()
        if reserved is not None:
            return str(reserved)

    raise JutsuIdAllocationExhausted("Could not allocate an identifier. Please try again.")


async def _record_event(
    session: AsyncSession, *, digest: bytes, domain: str | None, outcome: str
) -> None:
    """The trail for everything that happens before an organisation exists.

    `audit_log.org_id` is NOT NULL under FORCE RLS, so nothing can be written there yet.
    This sink holds the HMAC and the claimed domain and nothing else — never a name and
    never an address (§4.9).
    """
    await session.execute(
        text("SELECT auth.record_registration_event(:d, :dom, :o)"),
        {"d": digest, "dom": domain, "o": outcome},
    )


async def stage_registration(
    session: AsyncSession,
    request: RegistrationRequest,
    *,
    settings: Settings,
    sender: EmailSender,
) -> PendingRegistration:
    """Record the intent, send the code. Creates no organisation and no user.

    **The work email no longer has to sit on the organisation's domain.** It used to, and
    refusing the pair here saved a registrant ten minutes and an email for what was
    usually a typo. It also refused every founder whose company has no mail on its own
    domain yet, which is most of them before the first hire, and every pilot run from a
    personal address. The address is now taken as given and the code goes to it.

    What that costs is stated plainly rather than papered over: the domain on an
    organisation is no longer proof of anything by itself, so `domain_verified_at` is set
    at completion *only* when the verified address is actually on that domain. Anything
    that grants authority from a domain — auto-join, directory claims — must read that
    column and not the domain, or it inherits an assumption this function stopped
    honouring. Nothing does today; the constraint is written down so the next thing does
    not get it wrong.
    """
    # Both parses are guarded. `canonical_domain` raising here escaped as an unhandled
    # exception — `DomainError` is not a `JutsuError`, so it fell through to the
    # catch-all and a domain typed without a dot returned a 500 rather than "that does
    # not look like a domain". Confirmed against production before this was changed.
    try:
        email = normalise_email(request.work_email)
    except DomainError as exc:
        raise InvalidDomain("That does not look like an email address.") from exc

    try:
        domain = canonical_domain(request.company_domain)
    except DomainError as exc:
        raise InvalidDomain(_INVALID_DOMAIN_MESSAGE) from exc

    digest = email_hmac(email, settings)

    # Before any mail is sent. Staging delivers a message to an address the caller names,
    # which without a budget is an open relay — and each one also writes an identity row
    # and a staged payload. Keyed on the HMAC, so the ledger never becomes a list of
    # addresses by another name.
    remaining = (
        await session.execute(
            text("SELECT auth.spend_registration_budget(:s, :w, :l)"),
            {
                "s": digest,
                "w": timedelta(seconds=REGISTRATION_BUDGET_WINDOW_SECONDS),
                "l": REGISTRATION_BUDGET_LIMIT,
            },
        )
    ).scalar_one()
    if remaining < 0:
        await _record_event(session, digest=digest, domain=domain, outcome="throttled")
        raise TooManyRegistrations("Too many attempts for this address. Please try again later.")

    # `known_account=True` unconditionally: a registrant needs the code, and branching on
    # whether the address already has a membership would make the message body an
    # account-existence oracle that the identical 202 was designed to close.
    issued = await issue_challenge(
        session,
        address=email,
        purpose=ChallengePurpose.REGISTER,
        settings=settings,
        sender=sender,
        known_account=True,
        # Echoed into the message so a mistyped company or domain is caught before a
        # tenant is built around it. These are the values from this request and nothing
        # else — there is no organisation to look one up from, which is the entire point
        # of the staging half.
        organisation=(request.company_name.strip(), domain),
    )

    accepted_at = datetime.now(UTC)
    payload = {
        **asdict(request),
        "work_email": email,
        "company_domain": domain,
        # The moment the box was ticked, carried forward so the acceptance record can
        # state when consent happened rather than when it was persisted. The versions are
        # server constants — a client-supplied version would let the browser name a
        # document it never showed.
        "terms": {
            "accepted_at": accepted_at.isoformat(),
            "documents": TERMS_DOCUMENTS,
        },
    }

    await session.execute(
        text("SELECT auth.stage_registration(:t, :c, :d, :dom, CAST(:p AS jsonb), :exp)"),
        {
            "t": token_digest(issued.token),
            "c": issued.challenge_id,
            "d": digest,
            "dom": domain,
            "p": json.dumps(payload),
            "exp": accepted_at + timedelta(seconds=CHALLENGE_TTL_SECONDS),
        },
    )

    await _record_event(session, digest=digest, domain=domain, outcome="staged")
    return PendingRegistration(token=issued.token, challenge_id=issued.challenge_id)


async def complete_registration(
    session: AsyncSession,
    *,
    token: str,
    identity_id: UUID,
    challenge_id: UUID,
    settings: Settings,
) -> RegistrationOutcome:
    """Create the tenant, atomically, for a challenge that has just been redeemed.

    Called only after `verify_challenge(..., expected_purpose=REGISTER)` has succeeded,
    so mailbox control at the address is established before the first row is written.

    Partially applying this set is worse than failing it: an organisation with no owner
    cannot be administered by anyone, and a JUTSU ID bound to a user that was rolled back
    is an id permanently spent on nobody.
    """
    staged = (
        await session.execute(
            text(
                "SELECT challenge_id, email_hmac, domain, payload "
                "FROM auth.consume_pending_registration(:t)"
            ),
            {"t": token_digest(token)},
        )
    ).first()

    # Zero rows is the rejection, and it covers expired, already consumed, and never
    # existed alike. Single-use is enforced by the UPDATE itself, so a resend that
    # produced a second live challenge cannot mint a second organisation.
    if staged is None:
        raise NoPendingRegistration("That registration link is no longer valid.")

    staged_challenge_id, staged_digest, domain, payload = staged

    # The payload is bound to one challenge and one identity. Neither can differ here
    # unless something has separated the token from the challenge that issued it, which
    # would be a bug rather than a user error — refuse rather than guess.
    if UUID(str(staged_challenge_id)) != challenge_id:
        raise NoPendingRegistration("That registration link is no longer valid.")

    request = RegistrationRequest(
        full_name=payload["full_name"],
        work_email=payload["work_email"],
        company_name=payload["company_name"],
        company_domain=payload["company_domain"],
        job_title=payload["job_title"],
        org_size=payload["org_size"],
        country=payload.get("country"),
        industry=payload.get("industry"),
    )

    # Whether the verified address actually proves this domain — recorded, not enforced.
    #
    # Evaluated against the consumed staging row rather than anything this request
    # carried, for the same reason the equality was checked here when it was a gate: a
    # value that only ever passes through the staging path is not a value any future
    # path is obliged to supply honestly.
    #
    # The outcome lands in `domain_verified_at`. A registrant on the domain gets a
    # timestamp; anyone else gets NULL and an organisation that works exactly the same
    # way otherwise. The distinction is kept because it is true, and because the moment
    # anything grants authority from a domain it needs a column that means "proven"
    # rather than a column that means "typed".
    try:
        domain_proven = domain_of(request.work_email) == canonical_domain(domain)
    except DomainError as exc:
        raise InvalidDomain(_INVALID_DOMAIN_MESSAGE) from exc

    if not domain_proven:
        await _record_event(
            session, digest=bytes(staged_digest), domain=domain, outcome="unverified_domain"
        )

    org_id = uuid4()
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )

    now = datetime.now(UTC)

    # A savepoint around the `orgs` INSERT alone. A unique violation would otherwise
    # poison the whole transaction, and widening the savepoint would let an unrelated
    # conflict be mislabelled as a duplicate domain.
    try:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO orgs (id, name, domain, size_band, status, country, "
                    "industry, domain_verified_at) "
                    "VALUES (:id, :name, :domain, :size, 'active', :country, :industry, "
                    ":verified_at)"
                ),
                {
                    "id": org_id,
                    "name": request.company_name.strip(),
                    "domain": domain,
                    "size": request.org_size,
                    "country": request.country,
                    "industry": request.industry,
                    # NULL unless the address that was actually verified sits on this
                    # domain. Writing `now` unconditionally would make the column a
                    # record of when the row was inserted, which it already has.
                    "verified_at": now if domain_proven else None,
                },
            )
    except IntegrityError as exc:
        if _is_domain_conflict(exc):
            await _record_event(
                session, digest=bytes(staged_digest), domain=domain, outcome="duplicate"
            )
            # Safe to say plainly: whoever is reading proved a mailbox at this exact
            # domain moments ago, so the existence of the organisation is not news to
            # them. No session and no membership — joining someone to a tenant they were
            # never invited to would be a far worse answer than a dead end.
            raise DomainAlreadyRegistered(
                f"{domain} is already registered. Ask an administrator there for an invitation."
            ) from exc
        raise

    jutsu_id = await allocate_jutsu_id(session, org_id=org_id, kind=JutsuIdKind.ADMIN)

    user_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, org_id, email, display_name, identity_id, jutsu_id, "
            "status, activated_at) "
            "VALUES (:id, :org, :email, :name, :identity, :jid, 'active', :now)"
        ),
        {
            "id": user_id,
            "org": org_id,
            "email": request.work_email,
            "name": request.full_name.strip(),
            "identity": identity_id,
            "jid": jutsu_id,
            "now": now,
        },
    )

    # Binds the reserved id to this user. A compare-and-set, so it cannot be claimed twice.
    await session.execute(
        text("SELECT auth.claim_jutsu_id(:jid, :org, :user)"),
        {"jid": jutsu_id, "org": org_id, "user": user_id},
    )

    # The first administrator is the Organization Owner — the only role that can close
    # the organisation, and the only one that cannot be left vacant.
    await session.execute(
        text("INSERT INTO user_roles (user_id, org_id, role_key) VALUES (:user, :org, :role)"),
        {"user": user_id, "org": org_id, "role": Role.OWNER.value},
    )

    await session.execute(
        text("SELECT auth.record_membership(:identity, :org, :user)"),
        {"identity": identity_id, "org": org_id, "user": user_id},
    )

    # `accepted_at` from the staged row, `recorded_at` from the database clock. Keeping
    # them separate is the difference between a record that says when someone consented
    # and one that says when we got round to writing it down.
    accepted_at = datetime.fromisoformat(payload["terms"]["accepted_at"])
    for document, version in payload["terms"]["documents"].items():
        await session.execute(
            text(
                "INSERT INTO terms_acceptances (id, org_id, user_id, document, version, "
                "accepted_at) VALUES (:id, :org, :user, :doc, :ver, :at)"
            ),
            {
                "id": uuid4(),
                "org": org_id,
                "user": user_id,
                "doc": document,
                "ver": version,
                "at": accepted_at,
            },
        )

    # `actor_id` is the opaque user id, never the email or the name (§4.9).
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', 'org.created', 'org', :rid, 'success')"
        ),
        {"org": org_id, "actor": str(user_id), "rid": str(org_id)},
    )

    await _record_event(session, digest=bytes(staged_digest), domain=domain, outcome="created")

    return RegistrationOutcome(
        created=True,
        org_id=org_id,
        user_id=user_id,
        jutsu_id=jutsu_id,
        org_name=request.company_name.strip(),
        org_domain=domain,
        owner_email=request.work_email,
        owner_role=Role.OWNER,
    )


class NoPendingRegistration(JutsuError):
    """The token resolves to no live staged registration.

    Deliberately the same status and shape as an invalid code: expired, already used and
    never existed are one answer, because telling them apart says which half to work on.
    """

    status_code = 401
    code = "unauthenticated"


class DomainAlreadyRegistered(JutsuError):
    status_code = 409
    code = "domain_registered"


def _is_domain_conflict(exc: IntegrityError) -> bool:
    """Whether this violation is the domain index and not something else.

    asyncpg surfaces the constraint name on the wrapped error. Falling back to a string
    search keeps the check working if the driver ever stops exposing it, without ever
    treating an unrelated conflict as a duplicate domain.
    """
    constraint = getattr(getattr(exc, "orig", None), "constraint_name", None)
    if constraint is not None:
        return bool(constraint == DOMAIN_CONSTRAINT)
    return DOMAIN_CONSTRAINT in str(exc)
