"""Roles and permissions — the control plane.

**Roles gate features; ACLs gate data. Never conflate them** (§17). Nothing in
`Permission` grants a document read, and nothing here can. An Organization Owner with no
ACL grant still sees zero evidence, because visibility is decided by
`document_acl.principal_id` matching a namespaced provider subject the caller holds in
`source_identities` (ADR 0010) — a mechanism this module cannot reach. That omission is
deliberate and load-bearing: there is no `memory:read_all` to hand out by accident, which
is the most likely way an onboarding feature could quietly breach the product invariant.

This module is the *authoring* copy. Migration 0002 seeds the same values into Postgres
and then revokes write access to those tables from the application role, so the database
is the runtime authority and a compromised request path cannot mint a role or widen a
permission — only a migration can. `test_rbac_catalogue.py` asserts the two are
identical, so they cannot drift.

Routes declare a `Permission`, never a `Role`. A route that names a role has to be
revisited every time the role list changes; a route that names a permission does not.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final

__all__ = [
    "ROLE_LABELS",
    "ROLE_PERMISSIONS",
    "ROLE_RANKS",
    "Permission",
    "Role",
    "outranks",
    "permissions_for",
    "role_label",
]


class Permission(StrEnum):
    """What a caller may do. Namespaced `subject:verb` so the set stays readable."""

    ORG_READ = "org:read"
    ORG_UPDATE = "org:update"
    ORG_DELETE = "org:delete"

    MEMBER_READ = "member:read"
    MEMBER_INVITE = "member:invite"
    MEMBER_UPDATE = "member:update"
    MEMBER_ASSIGN_ROLE = "member:assign_role"

    INTEGRATION_READ = "integration:read"
    INTEGRATION_CONNECT = "integration:connect"
    INTEGRATION_REVOKE = "integration:revoke"

    AUDIT_READ = "audit:read"

    #: Knowledge-transfer lifecycle: create, list, revoke and complete packages.
    #: Holding it grants access to no document — a package scopes what its RECIPIENT
    #: may already read, and creating one moves no ACL row anywhere.
    KT_MANAGE = "kt:manage"

    #: Held by every role, including `member`. These are the things a person may always
    #: do to their *own* record — the resource check, not the permission, is what stops
    #: them doing it to someone else's.
    #:
    #: PROFILE_SELF_READ gates GET /v1/me. It exists because that endpoint was gated on
    #: ORG_READ, which `member` does not hold, so an invited employee could authenticate
    #: and then be refused their own identity. Reading who you are is not an
    #: organisational privilege.
    PROFILE_SELF_READ = "profile:self_read"
    PROFILE_SELF_UPDATE = "profile:self_update"
    INTEGRATION_SELF_MANAGE = "integration:self_manage"

    #: Retrieval, held by every role. §17: roles gate features, ACLs gate data.
    #:
    #: **It names the query, not the evidence, and that is deliberate.** It was first
    #: written `evidence:read` and `test_no_permission_grants_document_visibility`
    #: refused it — correctly. A permission spelled after a data-plane object reads as
    #: a grant over that object, and the next person to add one would follow the
    #: precedent. This permits *issuing a retrieval query*; which chunks come back is
    #: decided by `document_acl` inside the SQL, per caller, per request. Holding it
    #: grants access to no document, and no permission in this enum ever will.
    RETRIEVAL_QUERY = "retrieval:query"

    #: Open a KT package addressed to you. See _EVERYONE for why every role holds it.
    KT_OPEN = "kt:open"


class Role(StrEnum):
    OWNER = "owner"
    SUPER_ADMIN = "super_admin"
    HR_ADMIN = "hr_admin"
    IT_ADMIN = "it_admin"
    ANALYST = "analyst"
    VIEWER = "viewer"
    MEMBER = "member"


#: Spaced, and deliberately NOT unique.
#:
#: The escalation rule is strict — an actor may only grant a role ranked *below* their
#: own — so equal ranks express genuine peers. HR Admin and IT Admin have disjoint powers
#: and neither may promote anyone into the other. Forcing a unique rank would invent a
#: total order that does not exist and hand one of them authority over the other. The
#: gaps leave room to insert a role later without renumbering every row.
ROLE_RANKS: Final[MappingProxyType[Role, int]] = MappingProxyType(
    {
        Role.OWNER: 100,
        Role.SUPER_ADMIN: 80,
        Role.HR_ADMIN: 60,
        Role.IT_ADMIN: 60,
        Role.ANALYST: 40,
        Role.VIEWER: 20,
        Role.MEMBER: 10,
    }
)

#: How a role is written when a person reads it.
#:
#: Here rather than at the call site because the mechanical transform is wrong for half
#: of them: `hr_admin.title()` is "Hr Admin", and an email telling somebody they joined
#: as an "It Admin" reads as a typo in a message whose whole job is to look legitimate.
#: The first place that needed these was the welcome email; the admin console will want
#: the same strings, and two spellings of a role name is exactly the drift that makes a
#: product feel unfinished.
ROLE_LABELS: Final[MappingProxyType[Role, str]] = MappingProxyType(
    {
        Role.OWNER: "Organisation Owner",
        Role.SUPER_ADMIN: "Super Admin",
        Role.HR_ADMIN: "HR Admin",
        Role.IT_ADMIN: "IT Admin",
        Role.ANALYST: "Analyst",
        Role.VIEWER: "Viewer",
        Role.MEMBER: "Member",
    }
)


def role_label(role: Role) -> str:
    """The human spelling of a role.

    A direct lookup, with no fallback: `ROLE_LABELS` is exhaustive over the enum and the
    test asserts that it stays so. A `.get(role, role.value)` here would let a role added
    without a label ship silently as `hr_admin` in customer-facing mail.
    """
    return ROLE_LABELS[role]


_EVERYONE = (
    Permission.PROFILE_SELF_READ,
    Permission.PROFILE_SELF_UPDATE,
    Permission.INTEGRATION_SELF_MANAGE,
    Permission.RETRIEVAL_QUERY,
    #: Opening a knowledge-transfer package addressed to you. In everyone's set because
    #: the typical recipient is a brand-new Member — gating this on an admin permission
    #: would make the one person KT exists for the one person who cannot open it. The
    #: package's own binding (recipient, expiry, revocation) is the actual gate.
    Permission.KT_OPEN,
)

ROLE_PERMISSIONS: Final[MappingProxyType[Role, frozenset[Permission]]] = MappingProxyType(
    {
        Role.OWNER: frozenset(Permission),
        # Everything the owner has except closing the organisation — the one power the
        # role exists to withhold.
        Role.SUPER_ADMIN: frozenset(set(Permission) - {Permission.ORG_DELETE}),
        Role.HR_ADMIN: frozenset(
            {
                Permission.ORG_READ,
                Permission.MEMBER_READ,
                Permission.MEMBER_INVITE,
                Permission.MEMBER_UPDATE,
                Permission.MEMBER_ASSIGN_ROLE,
                Permission.AUDIT_READ,
                # Knowledge transfer is a people-transition act — offboarding, role
                # changes, onboarding — which is HR's domain, not IT's.
                Permission.KT_MANAGE,
                *_EVERYONE,
            }
        ),
        Role.IT_ADMIN: frozenset(
            {
                Permission.ORG_READ,
                Permission.ORG_UPDATE,
                Permission.MEMBER_READ,
                Permission.INTEGRATION_READ,
                Permission.INTEGRATION_CONNECT,
                Permission.INTEGRATION_REVOKE,
                Permission.AUDIT_READ,
                *_EVERYONE,
            }
        ),
        Role.ANALYST: frozenset(
            {
                Permission.ORG_READ,
                Permission.MEMBER_READ,
                Permission.INTEGRATION_READ,
                *_EVERYONE,
            }
        ),
        Role.VIEWER: frozenset({Permission.ORG_READ, *_EVERYONE}),
        Role.MEMBER: frozenset(_EVERYONE),
    }
)


def permissions_for(role: Role) -> frozenset[Permission]:
    """What a role may do. The runtime check reads the database, not this."""
    return ROLE_PERMISSIONS[role]


def outranks(actor: Role, target: Role) -> bool:
    """Whether `actor` may act on someone holding `target`.

    STRICTLY greater, so peers cannot act on each other and nobody can act on their own
    level. This is what stops an HR Admin deactivating an IT Admin, and — more
    importantly — what stops any admin promoting someone to their own rank and then being
    outranked by them.

    Note the caller still has to *use* this. It is needed in assign-role, deactivate,
    update, invite and revoke; a route that loads a target member and forgets the check
    is the escalation bug, not a missing rule.
    """
    return ROLE_RANKS[actor] > ROLE_RANKS[target]
