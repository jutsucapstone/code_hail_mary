"""Source identity lifecycle — linking, listing and revoking ACL principals (ADR 0010).

**Linking a source identity is granting document access.** That is the whole reason this
module is careful: `document_acl.principal_id` matches `{source_system}:{subject}`, so a
row here is the difference between a caller seeing a document and not seeing it. Every
control below exists because of that one sentence.

Two ways a row is created, and they are not equally trusted:

  * **Automatically, from a verified mailbox.** Registration and invitation acceptance both
    prove control of an address before they create a user — an OTP redeemed, or a
    single-use token that reached that address and nowhere else. For `SourceSystem.LOCAL`
    the subject namespace *is* email (ADR 0008), so `local:{verified_email}` is a mapping
    JUTSU has actually proven rather than one it assumed. This is the only link the system
    makes on its own.
  * **By an administrator, deliberately and audibly.** Any other subject — a Slack member
    id, an Atlassian `accountId` — is a claim nobody has verified, so it requires
    `integration:connect`, a rank check, and an audit row naming the actor.

**An administrator may not link themselves to an arbitrary subject.** §17 divides the
world: roles gate features, ACLs gate data, and `rbac.py` states that nothing in
`Permission` can grant a document read. Self-linking would make that false — a role would
become a way to hand yourself data access. It is refused, and that refusal is the single
most important line in this file.

**No OAuth, no tokens, no provider credentials.** Those are Phase 4. What ships here is the
lifecycle; a provider flow later becomes a new *source* of subjects feeding the same table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from jutsu_core import SourceSystem
from jutsu_core.errors import Conflict, NotFound, PermissionDenied
from jutsu_core.rbac import Role, outranks
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "LinkedIdentity",
    "link_identity",
    "link_verified_email",
    "list_identities",
    "revoke_all_for_user",
    "revoke_identity",
]

#: Recorded on the row as *how* the link was made. The *who* lives in `audit_log.actor_id`
#: — two different questions, deliberately in two different places, because the method is
#: a property of the row and the actor is an event.
LINKED_BY_VERIFIED_EMAIL = "verified_email"
LINKED_BY_ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class LinkedIdentity:
    id: UUID
    source_system: SourceSystem
    subject: str
    is_active: bool
    linked_at: datetime
    revoked_at: datetime | None
    linked_by: str


async def link_verified_email(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, verified_email: str
) -> None:
    """Link `local:{verified_email}` for a user whose mailbox has just been proven.

    Called from registration and from invitation acceptance, inside the transaction that
    creates the user. **The address must come from the verification, never from a request
    field** — both call sites pass the same value they wrote to `users.email`, which is
    the address an OTP or a single-use token actually reached.

    Idempotent. `ON CONFLICT DO NOTHING` on `(org_id, source_system, subject)` means
    re-running creates nothing, which matters because these two flows are the ones a
    retry or a double-submit is most likely to repeat.

    The conflict also covers a case worth naming: if that address is already linked to a
    *different* user in the same organisation, this silently links nothing and the new
    user holds no local principal. That is the correct outcome — one subject is one
    person per tenant — and it fails closed rather than moving somebody else's access.
    """
    await session.execute(
        text(
            "INSERT INTO source_identities "
            "(org_id, user_id, source_system, subject, linked_by) "
            "VALUES (:org, :user, CAST(:system AS source_system), :subject, :by) "
            "ON CONFLICT (org_id, source_system, subject) DO NOTHING"
        ),
        {
            "org": str(org_id),
            "user": str(user_id),
            "system": SourceSystem.LOCAL.value,
            "subject": verified_email,
            "by": LINKED_BY_VERIFIED_EMAIL,
        },
    )


async def list_identities(session: AsyncSession, *, user_id: UUID) -> list[LinkedIdentity]:
    """Every identity for one user, active and revoked, newest first.

    Revoked rows are included on purpose. A person asking what they are linked to deserves
    to see what they *were* linked to, and an administrator investigating an incident needs
    the revocation to be visible rather than absent.

    Scoped by row-level security through the session's GUC. No `org_id` parameter, for the
    same reason `scoped_acl_principals` has none: a tenant that can be named in a call is a
    tenant that can be named in a request.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id, source_system, subject, is_active, linked_at, revoked_at, linked_by "
                "FROM source_identities WHERE user_id = :u ORDER BY linked_at DESC, id"
            ),
            {"u": str(user_id)},
        )
    ).all()
    return [
        LinkedIdentity(
            id=UUID(str(row.id)),
            source_system=SourceSystem(row.source_system),
            subject=row.subject,
            is_active=row.is_active,
            linked_at=row.linked_at,
            revoked_at=row.revoked_at,
            linked_by=row.linked_by,
        )
        for row in rows
    ]


async def _target_role(session: AsyncSession, *, user_id: UUID) -> Role:
    """The role of the user being acted on, read inside the caller's tenant scope.

    A user in another organisation is invisible under RLS, so this raises `NotFound`
    rather than a permission error — the caller learns nothing about whether the id
    exists elsewhere.
    """
    role_key = (
        await session.execute(
            text("SELECT role_key FROM user_roles WHERE user_id = :u"), {"u": str(user_id)}
        )
    ).scalar_one_or_none()
    if role_key is None:
        raise NotFound("That employee was not found.")
    return Role(role_key)


async def _audit(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    action: str,
    resource_id: UUID,
) -> None:
    """One immutable row per ACL change (§17).

    `actor_id` is the opaque user id. Never the email, never the subject — §4.9 keeps
    both out of the audit trail, and the resource id is enough to find the row.
    """
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', :action, 'source_identity', :rid, 'success')"
        ),
        {
            "org": str(org_id),
            "actor": str(actor_id),
            "action": action,
            "rid": str(resource_id),
        },
    )


async def link_identity(
    session: AsyncSession,
    *,
    actor_id: UUID,
    actor_org_id: UUID,
    actor_role: Role,
    target_user_id: UUID,
    source_system: SourceSystem,
    subject: str,
) -> LinkedIdentity:
    """Link an arbitrary provider subject to a user. Administrator action.

    Four refusals, in the order they are cheapest to make:

    1. **Self-link.** An administrator linking themselves to a subject grants themselves
       that subject's documents, which would make a role a route to data access and
       breach the §17 separation this product rests on. Refused unconditionally — not
       gated on a permission, because there is no permission that should permit it.
    2. **Rank.** `outranks` is strict, so an HR Admin cannot link an IT Admin and nobody
       can act on a peer. Linking someone's identity is acting on their access; the
       ceiling that stops an invitation conferring a higher role stops this too.
    3. **Tenant.** The target is resolved under row-level security, so a user in another
       organisation is simply absent and the answer is `NotFound`.
    4. **Duplicate.** The unique constraint on `(org_id, source_system, subject)` means a
       subject already claimed in this tenant raises rather than moving silently.

    The caller's permission (`integration:connect`) is enforced by the route declaration,
    which `get_principal` checks per request.
    """
    if actor_id == target_user_id:
        raise PermissionDenied(
            "You cannot link a source identity to your own account. Ask another administrator."
        )

    target_role = await _target_role(session, user_id=target_user_id)
    if not outranks(actor_role, target_role):
        # Deliberately vague, matching the invitation ceiling: naming the rule tells a
        # caller exactly which target to aim for instead.
        raise PermissionDenied("You cannot manage source identities for that employee.")

    cleaned = subject.strip()
    if not cleaned:
        raise Conflict("A source identity needs a subject.")

    row = (
        await session.execute(
            text(
                "INSERT INTO source_identities "
                "(org_id, user_id, source_system, subject, linked_by) "
                "VALUES (:org, :user, CAST(:system AS source_system), :subject, :by) "
                "ON CONFLICT (org_id, source_system, subject) DO NOTHING "
                "RETURNING id, source_system, subject, is_active, linked_at, revoked_at, "
                "linked_by"
            ),
            {
                "org": str(actor_org_id),
                "user": str(target_user_id),
                "system": source_system.value,
                "subject": cleaned,
                "by": LINKED_BY_ADMIN,
            },
        )
    ).first()

    if row is None:
        # `DO NOTHING` returns no row when the subject is already claimed in this tenant.
        # Reported rather than silently re-pointed: moving a subject from one person to
        # another is a transfer of access and must be an explicit revoke-then-link.
        raise Conflict("That subject is already linked in this organisation.")

    identity_id = UUID(str(row.id))
    await _audit(
        session,
        org_id=actor_org_id,
        actor_id=actor_id,
        action="identity.linked",
        resource_id=identity_id,
    )

    return LinkedIdentity(
        id=identity_id,
        source_system=SourceSystem(row.source_system),
        subject=row.subject,
        is_active=row.is_active,
        linked_at=row.linked_at,
        revoked_at=row.revoked_at,
        linked_by=row.linked_by,
    )


async def revoke_identity(
    session: AsyncSession,
    *,
    actor_id: UUID,
    actor_org_id: UUID,
    actor_role: Role,
    target_user_id: UUID,
    identity_id: UUID,
) -> None:
    """Deactivate one identity. Takes effect on the caller's next request.

    A flag, not a delete: the row survives so the audit trail can still explain what
    access existed and when it stopped. `scoped_acl_principals` filters on `is_active`,
    and it re-reads on every request, so there is nothing to invalidate — which is what
    §17 test 3 demands of a group removal and applies equally here.

    Self-revocation is **allowed**. Removing your own access is not an escalation, and
    refusing it would leave somebody unable to undo a link they no longer want.
    """
    if actor_id != target_user_id:
        target_role = await _target_role(session, user_id=target_user_id)
        if not outranks(actor_role, target_role):
            raise PermissionDenied("You cannot manage source identities for that employee.")

    row = (
        await session.execute(
            text(
                "UPDATE source_identities SET is_active = false, revoked_at = now(), "
                "updated_at = now() "
                "WHERE id = :id AND user_id = :u AND is_active RETURNING id"
            ),
            {"id": str(identity_id), "u": str(target_user_id)},
        )
    ).first()

    if row is None:
        # Already revoked, never existed, or belongs to another tenant — one answer for
        # all three, so the endpoint cannot be used to probe which identities exist.
        raise NotFound("That source identity was not found.")

    await _audit(
        session,
        org_id=actor_org_id,
        actor_id=actor_id,
        action="identity.revoked",
        resource_id=identity_id,
    )


async def revoke_all_for_user(
    session: AsyncSession, *, actor_id: UUID, actor_org_id: UUID, target_user_id: UUID
) -> int:
    """Revoke every active identity a user holds. Returns how many were closed.

    The offboarding primitive. Deactivating a person must remove their access, and doing
    it identity by identity leaves a window in which some are gone and some are not.

    **It has no production caller yet**, and that is stated rather than hidden: there is
    no employee-deactivation route in the product today. The route that lands it will call
    this rather than reimplementing the loop, which is why the audit rows are written here
    and not there.

    No rank check: the only callers are an offboarding flow that has already made its own
    authorisation decision, and a test. It is not reachable from a route.
    """
    rows = (
        await session.execute(
            text(
                "UPDATE source_identities SET is_active = false, revoked_at = now(), "
                "updated_at = now() WHERE user_id = :u AND is_active RETURNING id"
            ),
            {"u": str(target_user_id)},
        )
    ).all()

    for row in rows:
        await _audit(
            session,
            org_id=actor_org_id,
            actor_id=actor_id,
            action="identity.revoked",
            resource_id=UUID(str(row.id)),
        )
    return len(rows)
