"""Passwordless authentication: issue a challenge, verify it, open a session.

The OTP is the primary factor and the magic link is a convenience. Both live on the same
challenge row, so redeeming either spends the same single use.

**The link is consumed by a POST, never by the GET that opens it.** Mail scanners, link
previewers and corporate security proxies fetch every URL in a message. If the GET
redeemed the challenge, a scanner would burn it before the recipient clicked, and — worse
— a redeemed link in a scanner's logs is a credential someone else already used. The
landing page therefore reads the token and submits it.

**Enumeration is closed by doing the same work, not by wording the response carefully.**
A request for an unknown address still upserts an identity, still writes a challenge,
still sends a message and still returns 202. The message body differs — an unknown
address is told there is no account and invited to register — but nothing observable over
HTTP does, including how long it took. An identity row is not an account: signing in also
requires a membership, so creating one for an unknown address grants nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from jutsu_core.errors import Unauthenticated
from jutsu_core.rbac import Permission, Role, permissions_for
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.config import (
    CHALLENGE_TTL_SECONDS,
    OTP_DIGITS,
    OTP_MAX_ATTEMPTS,
    SESSION_ABSOLUTE_TTL_SECONDS,
    SESSION_IDLE_TTL_SECONDS,
    Settings,
)
from jutsu_api.email import EmailMessage, EmailSender
from jutsu_api.emails import no_account, organisation_verification, sign_in_code
from jutsu_api.security import Principal

__all__ = [
    "ChallengePurpose",
    "IssuedChallenge",
    "RedeemedChallenge",
    "SessionCredentials",
    "email_hmac",
    "issue_challenge",
    "load_csrf_hash",
    "open_session",
    "resolve_principal",
    "revoke_session",
    "token_digest",
    "verify_challenge",
]


class ChallengePurpose:
    #: Mirrored by `ck_login_challenges_purpose` in migration 0007. A value that is not
    #: one of these is a write failure rather than a row nothing will ever match.
    SIGN_IN = "sign_in"
    REGISTER = "register"


@dataclass(frozen=True, slots=True)
class IssuedChallenge:
    challenge_id: UUID
    #: Returned only so a test or the console transport can complete the flow. Never
    #: placed in a response body and never logged.
    token: str
    code: str


@dataclass(frozen=True, slots=True)
class RedeemedChallenge:
    """What a successful redemption proves.

    The challenge id is returned, not just the identity, because it is the only value
    that ties this redemption to one specific issued challenge. Registration keys its
    staged payload on the token that produced this, and an identity is far too broad a
    key: two challenges for one address would otherwise be interchangeable.
    """

    identity_id: UUID
    challenge_id: UUID
    purpose: str


@dataclass(frozen=True, slots=True)
class SessionCredentials:
    """What the browser is given. Both are opaque; neither carries a claim."""

    token: str
    csrf_token: str
    expires_at: datetime


def email_hmac(address: str, settings: Settings) -> bytes:
    """The org-less lookup key for an address.

    HMAC rather than a plain hash, because a bare SHA-256 of an email is trivially
    reversible against a wordlist of known addresses — the input space is small and
    public. The pepper makes the table useless without a secret the database does not
    hold, which matters because `auth` sits outside every tenant boundary.

    Normalised first so `Ada@Example.COM ` and `ada@example.com` resolve to one identity.
    Case is folded on the whole address: the local part is technically case-sensitive per
    RFC 5321, and in practice no provider treats it that way — honouring the RFC here
    would create two accounts for one person.
    """
    normalised = address.strip().lower().encode("utf-8")
    return hmac.new(settings.email_pepper, normalised, hashlib.sha256).digest()


def _hash(value: str) -> bytes:
    """SHA-256 of a high-entropy secret.

    Correct precisely because these values are CSPRNG output, not chosen by a person:
    there is no dictionary to attack, so a work factor would cost every request and buy
    nothing. The OTP is the exception in principle — six digits is a tiny space — but it
    is only reachable through a definer function with a transactional attempt budget, so
    the guessing bound is enforced by the database rather than by hashing cost.
    """
    return hashlib.sha256(value.encode("ascii")).digest()


def token_digest(token: str) -> bytes:
    """The stored form of a magic-link token.

    Public because registration keys its staged payload on exactly this value: the row is
    reachable only by someone holding the token the message carried. Sharing one
    implementation is the point — a second hash of the same token computed slightly
    differently would silently never match, and the symptom would be "your code is not
    valid" for every legitimate registrant.
    """
    return _hash(token)


def _generate_code() -> str:
    """A zero-padded numeric OTP.

    Digits only: it gets typed on a phone keypad and read aloud. `randbelow` over the
    whole range rather than digit-by-digit choice, so every code is equally likely.
    """
    return f"{secrets.randbelow(10**OTP_DIGITS):0{OTP_DIGITS}d}"


def _challenge_message(
    *,
    address: str,
    purpose: str,
    settings: Settings,
    known_account: bool,
    organisation: tuple[str, str] | None,
) -> tuple[EmailMessage, bool]:
    """Pick the branded template, and say whether it expects the one-time values.

    Three outcomes, and the branch is total rather than defaulted, because the difference
    between them is what each message is allowed to say. A registration mail names the
    organisation about to be created; a sign-in mail names nothing at all, because at
    this point nobody has established that the address belongs to an account, and a
    message that named an organisation would answer in the recipient's inbox the question
    the identical 202 exists to leave unanswered.

    Neither carries an organisation identifier: `organisation_verification` is reached
    before any tenant exists and `sign_in_code` has no parameter for one. The only
    message that does is `organisation_welcome`, sent from the completion path.

    **The flag is returned rather than re-derived by the caller, and that is why.**
    Whether a template contains `[[code]]` and whether the send attaches a code are one
    decision, and they were briefly two: the caller keyed the secrets off `known_account`
    alone, so a registration challenge issued with `known_account=False` would have
    selected a template full of placeholders and then delivered it with nothing to fill
    them — an email reading `[[code]]` where the code belongs. Only `stage_registration`
    issues those and it always passes `True`, so it was unreachable; returning the pair
    makes it unrepresentable instead.
    """
    minutes = CHALLENGE_TTL_SECONDS // 60

    if purpose == ChallengePurpose.REGISTER:
        if organisation is None:
            # Unreachable from `stage_registration`, which always supplies it. A
            # registration mail with no company on it would still deliver a working code,
            # so this raises rather than falling back: a wrong-but-functional template is
            # the kind of defect that ships.
            raise ValueError("a registration challenge must name the organisation being created")
        company_name, company_domain = organisation
        message = organisation_verification(
            to=address,
            company_name=company_name,
            company_domain=company_domain,
            app_url=settings.app_url,
            minutes=minutes,
        )
        # A registrant always gets a code — that is the entire purpose of the message —
        # regardless of what the membership lookup would have said about the address.
        return message, True

    if known_account:
        return sign_in_code(to=address, app_url=settings.app_url, minutes=minutes), True
    return no_account(to=address, app_url=settings.app_url), False


async def issue_challenge(
    session: AsyncSession,
    *,
    address: str,
    purpose: str,
    settings: Settings,
    sender: EmailSender,
    known_account: bool | None = None,
    organisation: tuple[str, str] | None = None,
) -> IssuedChallenge:
    """Create a challenge and deliver it. Always does the same work.

    `known_account` lets the caller skip a lookup it has already done (registration knows
    the answer). It changes only the wording of the message.

    `organisation` is `(name, domain)` and is required for — and only accepted by — a
    registration challenge, where it is echoed back so a typo in either is caught before
    a tenant is built around it. It is the values the caller just typed, not a lookup:
    nothing has been created yet for there to be a lookup of.
    """
    digest = email_hmac(address, settings)
    identity_id = (
        await session.execute(text("SELECT auth.upsert_identity(:d)"), {"d": digest})
    ).scalar_one()

    token = secrets.token_urlsafe(32)
    code = _generate_code()
    expires_at = datetime.now(UTC) + timedelta(seconds=CHALLENGE_TTL_SECONDS)

    challenge_id = (
        await session.execute(
            text(
                "SELECT auth.create_challenge(:identity, :digest, :purpose, :token, "
                ":code, :expires, :attempts)"
            ),
            {
                "identity": identity_id,
                "digest": digest,
                "purpose": purpose,
                "token": _hash(token),
                "code": _hash(code),
                "expires": expires_at,
                "attempts": OTP_MAX_ATTEMPTS,
            },
        )
    ).scalar_one()

    if known_account is None:
        memberships = (
            await session.execute(
                text("SELECT org_id FROM auth.resolve_memberships(:i)"), {"i": identity_id}
            )
        ).all()
        known_account = bool(memberships)

    message, carries_credential = _challenge_message(
        address=address,
        purpose=purpose,
        settings=settings,
        known_account=known_account,
        organisation=organisation,
    )

    # The template holds `[[code]]` and `[[token]]`; these are the values the transport
    # substitutes into them. The flag comes back from the same branch that chose the
    # template, so a message with placeholders cannot be sent with nothing to fill them —
    # and the "no account" message, which has no placeholders, is sent with an empty
    # mapping so there is nothing that could leak into one.
    await sender.send(
        replace(message, secrets={"code": code, "token": token} if carries_credential else {})
    )

    return IssuedChallenge(challenge_id=challenge_id, token=token, code=code)


async def verify_challenge(
    session: AsyncSession, *, token: str, code: str, expected_purpose: str
) -> RedeemedChallenge:
    """Spend one attempt, check the code, and consume the challenge.

    Three statements, each a single atomic operation, in this order for a reason: the
    attempt is spent *before* the comparison, so a wrong guess costs the attacker budget
    whatever happens next. Doing it the other way round — compare, then record a failure
    — lets a client that disconnects mid-request guess for free.

    **The spend is committed before the rejection is raised, and that is the whole
    control.** `get_db` wraps each request in a single transaction, so raising out of
    here rolled `auth.consume_attempt` back with everything else and the budget never
    depleted — six digits with unlimited guesses is a five-minute brute force. The
    existing test passed only because it committed by hand between attempts, which
    production does not do. Committing here costs nothing else: at this point in a
    request the transaction contains the attempt spend and nothing more.

    `expected_purpose` is mandatory rather than defaulted. Sign-in and registration now
    redeem from one challenge namespace, and a default would silently hand whichever
    caller forgot to pass it the right to consume the other's codes. The refusal is the
    same sentence as every other, or the error itself would reveal which challenges are
    registrations.
    """
    attempt = (
        await session.execute(
            text(
                "SELECT challenge_id, identity_id, code_hash, purpose FROM auth.consume_attempt(:t)"
            ),
            {"t": _hash(token)},
        )
    ).first()

    # One error for every rejection: unknown token, already used, expired, out of
    # attempts, or issued for something else. Distinguishing them would tell an attacker
    # which half to work on.
    if attempt is None:
        raise Unauthenticated("That code is not valid.")

    challenge_id, identity_id, code_hash, purpose = attempt

    if not hmac.compare_digest(bytes(code_hash), _hash(code)) or not hmac.compare_digest(
        str(purpose).encode(), expected_purpose.encode()
    ):
        # Persist the spent attempt before unwinding. Without this the budget is a
        # decoration; see the docstring.
        await session.commit()
        raise Unauthenticated("That code is not valid.")

    consumed = (
        await session.execute(text("SELECT auth.consume_challenge(:c)"), {"c": challenge_id})
    ).scalar_one_or_none()
    if consumed is None:
        # Lost a race against a concurrent redemption of the same challenge. The other
        # request got the session; this one must not also get one.
        raise Unauthenticated("That code is not valid.")

    return RedeemedChallenge(
        identity_id=UUID(str(identity_id)),
        challenge_id=UUID(str(challenge_id)),
        purpose=str(purpose),
    )


async def open_session(
    session: AsyncSession, *, identity_id: UUID, user_id: UUID, org_id: UUID
) -> SessionCredentials:
    """Mint an opaque session handle and its CSRF partner.

    Neither value carries a claim. The org is recorded on the row, server-side, which is
    what lets the cookie stay claim-free — there is nothing in it for a frontend route to
    read and act on.
    """
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=SESSION_ABSOLUTE_TTL_SECONDS)

    await session.execute(
        text("SELECT auth.create_session(:identity, :user, :org, :token, :csrf, :expires, :idle)"),
        {
            "identity": identity_id,
            "user": user_id,
            "org": org_id,
            "token": _hash(token),
            "csrf": _hash(csrf_token),
            "expires": expires_at,
            "idle": now + timedelta(seconds=SESSION_IDLE_TTL_SECONDS),
        },
    )

    return SessionCredentials(token=token, csrf_token=csrf_token, expires_at=expires_at)


async def scoped_role(session: AsyncSession, *, org_id: object, user_id: object) -> Role:
    """The role a user holds, read inside the tenant scope that makes it meaningful.

    The two statements are ordered, and the order is the tenancy guarantee rather than a
    style preference: `user_roles` is behind RLS keyed on `app.current_org_id`, so reading
    a role before the GUC is set reads it across every tenant. Anything that needs a role
    goes through here so that ordering exists once instead of at each call site — sign-in
    needs it to decide where to send someone, and that is exactly the kind of second
    implementation that drifts away from the first.
    """
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )
    role_key = (
        await session.execute(
            text("SELECT role_key FROM user_roles WHERE user_id = :u"), {"u": user_id}
        )
    ).scalar_one_or_none()
    if role_key is None:
        # A user with no role is not a caller we can authorise. It means the membership
        # was removed, or a registration failed part-way — either way, refuse rather than
        # defaulting to something.
        raise Unauthenticated("Your access has been withdrawn.")
    return Role(role_key)


async def resolve_principal(session: AsyncSession, *, token: str) -> Principal:
    """Turn an opaque handle into an authenticated caller.

    Two steps, and the order is the tenancy guarantee. `auth.resolve_session` is org-less
    and yields the organisation; only then is the GUC set and the role read under RLS. A
    role loaded before scoping would be read across every tenant.
    """
    row = (
        await session.execute(
            text(
                "SELECT session_id, identity_id, user_id, org_id, csrf_hash "
                "FROM auth.resolve_session(:t)"
            ),
            {"t": _hash(token)},
        )
    ).first()
    if row is None:
        raise Unauthenticated("Your session has expired.")

    session_id, identity_id, user_id, org_id, _csrf_hash = row

    role = await scoped_role(session, org_id=org_id, user_id=user_id)
    return Principal(
        session_id=UUID(str(session_id)),
        identity_id=UUID(str(identity_id)),
        user_id=UUID(str(user_id)),
        org_id=UUID(str(org_id)),
        role=role,
        permissions=frozenset(permissions_for(role)),
    )


async def load_csrf_hash(session: AsyncSession, *, token: str) -> bytes | None:
    """The CSRF value paired with this session, for the double-submit comparison.

    Read separately from `resolve_principal` so the principal type carries no secret at
    all — a Principal that held the hash would eventually be logged or serialised by
    something, and this keeps that impossible by construction.
    """
    row = (
        await session.execute(
            text("SELECT csrf_hash FROM auth.resolve_session(:t)"), {"t": _hash(token)}
        )
    ).scalar_one_or_none()
    return bytes(row) if row is not None else None


async def revoke_session(session: AsyncSession, *, token: str) -> bool:
    """Sign out. Idempotent — signing out twice is not an error."""
    revoked = (
        await session.execute(text("SELECT auth.revoke_session(:t)"), {"t": _hash(token)})
    ).scalar_one_or_none()
    return revoked is not None


def capability_payload(principal: Principal) -> dict[str, object]:
    """What the frontend needs to decide what to *render*.

    Never what it needs to decide what to *allow*. Every one of these permissions is
    re-checked server-side on the call it gates; this exists so the UI can hide a button
    the caller cannot use, which is a courtesy, not a control.
    """
    return {
        "org_id": str(principal.org_id),
        "user_id": str(principal.user_id),
        "role": principal.role.value,
        "permissions": sorted(p.value for p in principal.permissions),
    }


def has_permission(principal: Principal, permission: Permission) -> bool:
    return principal.can(permission)
