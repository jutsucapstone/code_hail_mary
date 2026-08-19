"""Organisation registration.

One transaction creates the tenant, its first administrator, that person's role, their
JUTSU ID, the org-less membership index entry and the audit record. Partially applying
that set is worse than failing it: an organisation with no owner cannot be administered
by anyone, and a JUTSU ID bound to a user that was rolled back is an id permanently spent
on nobody.

**The scope is set before the organisation exists.** `orgs` carries a `WITH CHECK` policy
on `id`, so an unscoped INSERT is rejected — and a row cannot be scoped to itself before
it is written. The id is therefore minted here and the GUC set to it first. That ordering
looks odd until you try it the other way round, which is why the fixture in
`packages/db/tests/conftest.py` has the same shape.

**A duplicate domain never changes the HTTP response.** Telling the caller "that company
is already registered" is a customer-enumeration oracle: anyone could probe domains to
learn who uses JUTSU. The request returns the same 202 either way, and the difference is
carried in the email — which only reaches someone who controls that address.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from jutsu_core.errors import JutsuError
from jutsu_core.ids import JutsuIdKind, generate_jutsu_id
from jutsu_core.rbac import Role
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.auth_service import ChallengePurpose, email_hmac, issue_challenge
from jutsu_api.config import Settings
from jutsu_api.email import EmailSender

__all__ = [
    "RegistrationOutcome",
    "RegistrationRequest",
    "allocate_jutsu_id",
    "register_organisation",
]

#: Attempts before allocation is treated as broken rather than unlucky.
#:
#: Derived, not chosen by feel: the space is 32**8, so at 10**8 allocated ids the
#: per-insert collision rate is 9.09e-5 and five consecutive collisions have probability
#: 6.22e-21. Exhausting this loop is therefore not bad luck — it means the CSPRNG is
#: degraded or the ledger is being written by something else, both of which are incidents
#: that must surface rather than be papered over with a longer id.
JUTSU_ID_ATTEMPTS = 5


class JutsuIdAllocationExhausted(JutsuError):
    status_code = 503
    code = "service_unavailable"


@dataclass(frozen=True, slots=True)
class RegistrationRequest:
    full_name: str
    work_email: str
    company_name: str
    company_domain: str
    job_title: str
    org_size: str


@dataclass(frozen=True, slots=True)
class RegistrationOutcome:
    """What happened, for the caller's logging — never for the response body.

    `created` is False when the domain was already registered. The route must render an
    identical response either way.
    """

    created: bool
    org_id: UUID | None
    user_id: UUID | None
    jutsu_id: str | None


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


async def register_organisation(
    session: AsyncSession,
    request: RegistrationRequest,
    *,
    settings: Settings,
    sender: EmailSender,
) -> RegistrationOutcome:
    digest = email_hmac(request.work_email, settings)
    identity_id = (
        await session.execute(text("SELECT auth.upsert_identity(:d)"), {"d": digest})
    ).scalar_one()

    org_id = uuid4()
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )

    domain = request.company_domain.strip().lower()

    # A savepoint, because a unique violation would otherwise poison the whole
    # registration transaction and take the identity upsert with it.
    try:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO orgs (id, name, domain, size_band, status) "
                    "VALUES (:id, :name, :domain, :size, 'active')"
                ),
                {
                    "id": org_id,
                    "name": request.company_name.strip(),
                    "domain": domain,
                    "size": request.org_size,
                },
            )
    except IntegrityError:
        # The domain is already registered. Say nothing over HTTP; the email tells the
        # person what to do, and only they can read it.
        await issue_challenge(
            session,
            address=request.work_email,
            purpose=ChallengePurpose.SIGN_IN,
            settings=settings,
            sender=sender,
        )
        return RegistrationOutcome(created=False, org_id=None, user_id=None, jutsu_id=None)

    jutsu_id = await allocate_jutsu_id(session, org_id=org_id, kind=JutsuIdKind.ADMIN)

    user_id = uuid4()
    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO users (id, org_id, email, display_name, identity_id, jutsu_id, "
            "status, activated_at) "
            "VALUES (:id, :org, :email, :name, :identity, :jid, 'active', :now)"
        ),
        {
            "id": user_id,
            "org": org_id,
            "email": request.work_email.strip().lower(),
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

    # `actor_id` is the opaque user id, never the email or the name (§4.9).
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', 'org.created', 'org', :rid, 'success')"
        ),
        {"org": org_id, "actor": str(user_id), "rid": str(org_id)},
    )

    await issue_challenge(
        session,
        address=request.work_email,
        purpose=ChallengePurpose.REGISTER,
        settings=settings,
        sender=sender,
        known_account=True,
    )

    return RegistrationOutcome(created=True, org_id=org_id, user_id=user_id, jutsu_id=jutsu_id)
