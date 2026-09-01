"""Employee invitations: issue one, and accept one.

Acceptance is the mirror of registration — it creates a person inside an organisation
that already exists — and it has the same atomicity requirement. A half-applied
acceptance leaves either a user with no role, or a JUTSU ID spent on nobody, or an
invitation marked used against an account that was rolled back.

**The token is consumed by a single compare-and-set, before anything else happens.**
Reading the invitation and then marking it used is a race: two requests carrying the same
link both read it as unused, both create an account, and one JUTSU ID is silently
orphaned. One statement that filters and marks in the same breath makes the second
request see no row.

**Accepting proves control of the address**, because the token only ever reached that
inbox. So acceptance issues a session directly rather than sending a second code — the
invitation *is* the challenge. It is consumed by a POST from a landing page for the same
reason the magic link is: mail scanners fetch every URL in a message, and a GET that
accepted would let a scanner burn the invitation before the recipient ever clicked.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from jutsu_core.errors import Conflict, NotFound, PermissionDenied, Unauthenticated
from jutsu_core.ids import JutsuIdKind
from jutsu_core.rbac import Role, outranks
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.auth_service import email_hmac
from jutsu_api.config import Settings
from jutsu_api.email import EmailSender
from jutsu_api.emails import employee_invitation
from jutsu_api.identities import link_verified_email
from jutsu_api.registration import allocate_jutsu_id
from jutsu_api.security import Principal

__all__ = ["AcceptedInvitation", "IssuedInvitation", "accept_invitation", "invite_employee"]

#: Long enough to survive a weekend and a forwarded mail; short enough that a stale link
#: in an inbox is not a standing key to the organisation.
INVITATION_TTL_HOURS = 72

#: One sentence for both ends of the same refusal — issuing an invitation for an active
#: member, and accepting one for an address that became a member in the meantime.
_ALREADY_A_MEMBER = "That person is already a member of this organisation."


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    invitation_id: UUID
    #: Returned so the console transport and the tests can complete the flow. Never
    #: placed in a response body, never logged.
    token: str


@dataclass(frozen=True, slots=True)
class AcceptedInvitation:
    user_id: UUID
    org_id: UUID
    identity_id: UUID
    jutsu_id: str
    #: The role the invitation conferred. Carried out of here because the caller has to
    #: decide where to send this person, and re-reading it from `user_roles` afterwards
    #: would be a second query for something this function already had in hand.
    role: Role
    #: For the welcome message, and for nothing else. Both were in hand here — the name
    #: under the scope this function had already set, the address off the consumed
    #: invitation — so carrying them out avoids re-reading a tenant the caller has only
    #: just been admitted to.
    org_name: str
    email: str


def _hash(value: str) -> bytes:
    # UTF-8, not ASCII: the accept path hashes a token the request body supplied, and a
    # non-ASCII byte in it must be an ordinary unknown-token refusal, never an encode
    # crash. Every token this module mints is URL-safe ASCII, so the digests are
    # unchanged for anything that could ever match.
    return hashlib.sha256(value.encode("utf-8")).digest()


async def invite_employee(
    session: AsyncSession,
    *,
    actor: Principal,
    email: str,
    role: Role,
    settings: Settings,
    sender: EmailSender,
    role_title: str | None = None,
) -> IssuedInvitation:
    """Invite someone into the actor's organisation.

    The escalation ceiling is enforced here, not just on role assignment. An invitation
    that could confer a role the inviter does not outrank is the same privilege
    escalation as granting it directly, only slower and harder to notice — an HR Admin
    inviting someone as Owner would hand away the organisation.
    """
    if not outranks(actor.role, role):
        # Deliberately vague. Naming the ceiling would tell a caller exactly which role
        # to aim for next.
        raise PermissionDenied("You cannot invite someone at that level of access.")

    normalised = email.strip().lower()

    # Refused before anything is written. An invitation for an address that already
    # belongs to an active member would, at acceptance, create a second `users` row for
    # the same person — and `auth.record_membership` upserts on (identity, org), so the
    # duplicate would silently repoint sign-in at the new row and orphan the original
    # user's role, profile and identities. Row-level security scopes the lookup to the
    # actor's organisation; disclosing membership to a holder of `member:invite` is not
    # an oracle — the members list is already theirs to read.
    member = (
        await session.execute(
            text("SELECT 1 FROM users WHERE lower(email) = :email AND status = 'active'"),
            {"email": normalised},
        )
    ).scalar_one_or_none()
    if member is not None:
        raise Conflict(_ALREADY_A_MEMBER)

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(hours=INVITATION_TTL_HOURS)

    try:
        invitation_id = (
            await session.execute(
                text(
                    "INSERT INTO invitations "
                    "(id, org_id, email, role_key, token_hash, invited_by, expires_at, "
                    "role_title) "
                    "VALUES (:id, :org, :email, :role, :token, :by, :expires, :title) "
                    "RETURNING id"
                ),
                {
                    "id": uuid4(),
                    "org": actor.org_id,
                    "email": normalised,
                    "role": role.value,
                    "token": _hash(token),
                    "by": actor.user_id,
                    "expires": expires_at,
                    # A TITLE, not a role (§17): free text the inviter wrote, applied to
                    # the invitee's profile designation at acceptance. It confers no
                    # permission — role_key above is the closed-catalogue authority.
                    "title": (role_title or "").strip()[:128] or None,
                },
            )
        ).scalar_one()
    except IntegrityError as exc:
        # The partial unique index only covers live invitations, so this means one is
        # already outstanding — not that the address was ever invited before. Re-inviting
        # someone whose invitation was revoked or expired is allowed.
        raise Conflict("That person already has an invitation waiting.") from exc

    # The org-less index from token to organisation. Without it, acceptance — which runs
    # with no session and therefore no tenant scope — cannot see the invitation at all,
    # because RLS makes it invisible rather than merely unreadable.
    await session.execute(
        text("SELECT auth.record_invitation_token(:token, :id, :org)"),
        {"token": _hash(token), "id": invitation_id, "org": actor.org_id},
    )

    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', 'member.invited', 'invitation', :rid, 'success')"
        ),
        {"org": actor.org_id, "actor": str(actor.user_id), "rid": str(invitation_id)},
    )

    # Named, not "your organisation". An invitation that cannot say who it is from is
    # indistinguishable from a phishing mail, and the reader's only defence is to ignore
    # it. Read under the actor's own tenant scope, so the name can only ever be theirs.
    organisation = await _organisation_name(session, org_id=actor.org_id)

    message = employee_invitation(
        to=normalised,
        organisation=organisation,
        app_url=settings.app_url,
        hours=INVITATION_TTL_HOURS,
    )
    # The template carries `[[token]]` inside the accept link; this is the only place the
    # value itself exists outside the database hash.
    await sender.send(replace(message, secrets={"token": token}))

    return IssuedInvitation(invitation_id=invitation_id, token=token)


async def _organisation_name(session: AsyncSession, *, org_id: object) -> str:
    """The tenant's display name, for a message that has to say who it is from.

    Falls back rather than raising. A missing name here would mean an organisation row
    that row-level security cannot see from inside its own scope, which is a far larger
    problem than an unnamed invitation — but failing the invitation is not how anyone
    would want to discover it, and the invitee is not the person who can act on it.
    """
    name = (
        await session.execute(text("SELECT name FROM orgs WHERE id = :id"), {"id": org_id})
    ).scalar_one_or_none()
    return str(name) if name else "your organisation"


async def accept_invitation(
    session: AsyncSession, *, token: str, full_name: str, settings: Settings
) -> AcceptedInvitation:
    """Consume an invitation and create the person it was for.

    Runs unscoped until the organisation is known, because the invitee has no session and
    therefore no tenant — the invitation itself is what supplies one. The GUC is set from
    the invitation's own `org_id`, never from anything the caller sent.
    """
    # Two steps, and the order is the whole fix. The invitee has no session, so no tenant
    # scope — and `invitations` is under FORCE row-level security, which makes every row
    # invisible rather than merely unreadable. Resolving the organisation first, through
    # the org-less index, is what lets the consuming statement below see anything at all.
    located = (
        await session.execute(
            text("SELECT invitation_id, org_id FROM auth.resolve_invitation_token(:token)"),
            {"token": _hash(token)},
        )
    ).first()

    if located is None:
        raise Unauthenticated("That invitation is no longer valid.")

    invitation_id, org_id = located

    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )

    # Now scoped, so this sees the row. Still one statement: filter and mark together, so
    # two requests carrying the same link cannot both create an account — the second sees
    # nothing.
    consumed = (
        await session.execute(
            text(
                "UPDATE invitations SET accepted_at = now() "
                "WHERE id = :id AND accepted_at IS NULL "
                "AND revoked_at IS NULL AND expires_at > now() "
                "RETURNING email, role_key, role_title"
            ),
            {"id": invitation_id},
        )
    ).first()

    if consumed is None:
        # One refusal for every case — already used, revoked, expired. Telling them apart
        # would let someone probe which links were real.
        raise Unauthenticated("That invitation is no longer valid.")

    email, role_key, role_title = consumed

    # The invite side refuses to issue for an active member, but an invitation is a
    # standing token: the address can become a member between issue and acceptance.
    # Re-checked here, inside the transaction that would create the duplicate, so the
    # second `users` row — and the membership upsert that would repoint sign-in at it,
    # orphaning the original role, profile and identities — is unrepresentable. The
    # raise rolls the consuming UPDATE back with everything else.
    already = (
        await session.execute(
            text("SELECT 1 FROM users WHERE lower(email) = :email AND status = 'active'"),
            {"email": str(email).lower()},
        )
    ).scalar_one_or_none()
    if already is not None:
        raise Conflict(_ALREADY_A_MEMBER)

    identity_id = (
        await session.execute(
            text("SELECT auth.upsert_identity(:d)"),
            {"d": email_hmac(email, settings)},
        )
    ).scalar_one()

    # EMP regardless of the role granted. The prefix records how the person joined, not
    # what they may do — it is frozen at allocation and must never become an
    # authorisation input, so encoding a role in it would be actively misleading.
    jutsu_id = await allocate_jutsu_id(session, org_id=org_id, kind=JutsuIdKind.EMPLOYEE)

    user_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, org_id, email, display_name, identity_id, jutsu_id, "
            "status, activated_at) "
            "VALUES (:id, :org, :email, :name, :identity, :jid, 'active', now())"
        ),
        {
            "id": user_id,
            "org": org_id,
            "email": email,
            "name": full_name.strip(),
            "identity": identity_id,
            "jid": jutsu_id,
        },
    )

    # The address the invitation token reached, and nowhere else — the same proof an
    # emailed code would give. Taken from the consumed invitation, never from the request
    # body, which carries only a name (ADR 0010).
    await link_verified_email(session, org_id=org_id, user_id=user_id, verified_email=email)

    await session.execute(
        text("SELECT auth.claim_jutsu_id(:jid, :org, :user)"),
        {"jid": jutsu_id, "org": org_id, "user": user_id},
    )
    await session.execute(
        text("INSERT INTO user_roles (user_id, org_id, role_key) VALUES (:u, :o, :r)"),
        {"u": user_id, "o": org_id, "r": role_key},
    )

    # The written role title (§1's free-text option) lands as the person's profile
    # designation — display vocabulary they can edit later, never authority. The
    # permissions above came from the closed catalogue and nothing here widens them.
    if role_title:
        await session.execute(
            text(
                "INSERT INTO employee_profiles (user_id, org_id, designation) "
                "VALUES (:u, :o, :d) ON CONFLICT (user_id) DO UPDATE SET "
                "designation = EXCLUDED.designation"
            ),
            {"u": user_id, "o": org_id, "d": role_title},
        )
    await session.execute(
        text("SELECT auth.record_membership(:identity, :org, :user)"),
        {"identity": identity_id, "org": org_id, "user": user_id},
    )
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', 'member.activated', 'user', :rid, 'success')"
        ),
        {"org": org_id, "actor": str(user_id), "rid": str(user_id)},
    )

    return AcceptedInvitation(
        user_id=user_id,
        org_id=org_id,
        identity_id=identity_id,
        jutsu_id=jutsu_id,
        role=Role(role_key),
        org_name=await _organisation_name(session, org_id=org_id),
        email=str(email),
    )


async def list_employees(
    session: AsyncSession, *, limit: int, cursor: str | None, query: str | None
) -> tuple[list[dict[str, object]], str | None]:
    """People in the caller's organisation.

    Keyset pagination on `(created_at, id)` rather than OFFSET. Offset paging over a table
    that is being written to skips and duplicates rows between pages — and this table is
    written to precisely when an admin is looking at it, because that is when they are
    inviting people.

    No `org_id` predicate appears anywhere below. That is deliberate: row-level security
    supplies it, and adding a redundant one as "defence in depth" would mask a broken
    policy — the isolation test would still pass while the policy sat inert.
    """
    bounded = max(1, min(limit, 100))

    filters = ["true"]
    params: dict[str, object] = {"limit": bounded + 1}

    if cursor:
        try:
            created_at, last_id = cursor.split("|", 1)
            params["cursor_created_at"] = datetime.fromisoformat(created_at)
            params["cursor_id"] = UUID(last_id)
        except (ValueError, AttributeError) as exc:
            raise NotFound("That page does not exist.") from exc
        filters.append("(u.created_at, u.id) > (:cursor_created_at, :cursor_id)")

    if query:
        params["query"] = f"%{query.strip().lower()}%"
        filters.append("(lower(u.email) LIKE :query OR lower(u.display_name) LIKE :query)")

    # S608: every fragment joined into the WHERE clause is a literal defined above. The
    # caller's search text and cursor are bound parameters and never reach the SQL text.
    # Bandit cannot distinguish a constant from user input, so the exemption is narrow
    # and stated rather than applied to the file.
    rows = (
        await session.execute(
            text(
                "SELECT u.id, u.email, u.display_name, u.jutsu_id, u.status, "  # noqa: S608
                "u.created_at, u.last_activity_at, ur.role_key "
                "FROM users u LEFT JOIN user_roles ur ON ur.user_id = u.id "
                f"WHERE {' AND '.join(filters)} "
                "ORDER BY u.created_at, u.id LIMIT :limit"
            ),
            params,
        )
    ).all()

    has_more = len(rows) > bounded
    page = rows[:bounded]
    next_cursor = f"{page[-1].created_at.isoformat()}|{page[-1].id}" if has_more and page else None

    return (
        [
            {
                "id": str(row.id),
                "email": row.email,
                "display_name": row.display_name,
                "jutsu_id": row.jutsu_id,
                "status": row.status,
                "role": row.role_key,
                "created_at": row.created_at,
                "last_activity_at": row.last_activity_at,
            }
            for row in page
        ],
        next_cursor,
    )
