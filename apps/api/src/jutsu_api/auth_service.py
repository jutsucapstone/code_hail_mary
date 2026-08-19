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
from dataclasses import dataclass
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
from jutsu_api.security import Principal

__all__ = [
    "ChallengePurpose",
    "IssuedChallenge",
    "SessionCredentials",
    "email_hmac",
    "issue_challenge",
    "load_csrf_hash",
    "open_session",
    "resolve_principal",
    "revoke_session",
    "verify_challenge",
]


class ChallengePurpose:
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


def _generate_code() -> str:
    """A zero-padded numeric OTP.

    Digits only: it gets typed on a phone keypad and read aloud. `randbelow` over the
    whole range rather than digit-by-digit choice, so every code is equally likely.
    """
    return f"{secrets.randbelow(10**OTP_DIGITS):0{OTP_DIGITS}d}"


async def issue_challenge(
    session: AsyncSession,
    *,
    address: str,
    purpose: str,
    settings: Settings,
    sender: EmailSender,
    known_account: bool | None = None,
) -> IssuedChallenge:
    """Create a challenge and deliver it. Always does the same work.

    `known_account` lets the caller skip a lookup it has already done (registration knows
    the answer). It changes only the wording of the message.
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

    body = (
        f"Your JUTSU sign-in code is below. It expires in "
        f"{CHALLENGE_TTL_SECONDS // 60} minutes and can be used once."
        if known_account
        else (
            "Someone asked to sign in to JUTSU with this address, but it has no "
            "account. If that was you, you can register at /pilot."
        )
    )

    await sender.send(
        EmailMessage(
            to=address,
            subject="Your JUTSU sign-in code",
            body=body,
            secrets={"code": code, "token": token} if known_account else {},
        )
    )

    return IssuedChallenge(challenge_id=challenge_id, token=token, code=code)


async def verify_challenge(session: AsyncSession, *, token: str, code: str) -> UUID:
    """Spend one attempt, check the code, and consume the challenge. Returns the identity.

    Three statements, each a single atomic operation, in this order for a reason: the
    attempt is spent *before* the comparison, so a wrong guess costs the attacker budget
    whatever happens next. Doing it the other way round — compare, then record a failure
    — lets a client that disconnects mid-request guess for free.
    """
    attempt = (
        await session.execute(
            text("SELECT challenge_id, identity_id, code_hash FROM auth.consume_attempt(:t)"),
            {"t": _hash(token)},
        )
    ).first()

    # One error for every rejection: unknown token, already used, expired, or out of
    # attempts. Distinguishing them would tell an attacker which half to work on.
    if attempt is None:
        raise Unauthenticated("That code is not valid.")

    challenge_id, identity_id, code_hash = attempt
    if not hmac.compare_digest(bytes(code_hash), _hash(code)):
        raise Unauthenticated("That code is not valid.")

    consumed = (
        await session.execute(text("SELECT auth.consume_challenge(:c)"), {"c": challenge_id})
    ).scalar_one_or_none()
    if consumed is None:
        # Lost a race against a concurrent redemption of the same challenge. The other
        # request got the session; this one must not also get one.
        raise Unauthenticated("That code is not valid.")

    return UUID(str(identity_id))


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

    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )

    role_key = (
        await session.execute(
            text("SELECT role_key FROM user_roles WHERE user_id = :u"), {"u": user_id}
        )
    ).scalar_one_or_none()
    if role_key is None:
        # A session whose user has no role is not a caller we can authorise. It means the
        # membership was removed, or a registration failed part-way — either way, refuse
        # rather than defaulting to something.
        raise Unauthenticated("Your access has been withdrawn.")

    role = Role(role_key)
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
