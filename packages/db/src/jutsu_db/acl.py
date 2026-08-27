"""Resolving a user to the ACL principals a query may match (ADR 0010).

This lives in `jutsu_db` rather than in the API because two callers need the *same* answer
and a second copy of an authorization query is how the two drift. `apps/api` resolves
principals to put them on `Principal`; `jutsu_retrieval.search` resolves them to build the
§12 filter. One implementation, one set of tests, one thing to review.

Three properties are load-bearing, and all three are the reason this is a function rather
than a cached attribute:

**Eager, per call, never cached.** §17 test 3 requires a group removal to take effect on
the next query with no cache flush, and revoking a source identity must behave the same
way. A cache that outlives a revocation is an authorization decision made from stale
state — the failure that is hardest to notice and worst to explain. One indexed read is
the price.

**Only active identities.** `is_active` is the revocation switch. Offboarding flips it
rather than deleting the row, so the audit trail survives and authorization stops.

**The organisation is never an argument.** Both reads are scoped by row-level security
through `app.current_org_id`, which the session set from a *session-derived* org id. A
tenant that can be named in a call is a tenant that can be named in a request.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["resolve_acl_principals"]


async def resolve_acl_principals(
    session: AsyncSession, *, user_id: object
) -> tuple[frozenset[str], frozenset[str]]:
    """`(principals, groups)` for one user, inside whatever tenant the session is scoped to.

    Principals are namespaced `{source_system}:{subject}` so a Slack member id can never
    match a GitHub grant. Groups are stored already namespaced in `group_external_id`, in
    the same form and for the same reason.

    **Empty is the correct answer, not a failure.** A user with no linked identity matches
    no `user` or `group` grant, so `= ANY('{}')` is false for every row and they retrieve
    nothing. That is §17 test 1 — no grants means zero results, not an error.
    """
    identity_rows = (
        await session.execute(
            text(
                "SELECT source_system, subject FROM source_identities "
                "WHERE user_id = :u AND is_active"
            ),
            {"u": user_id},
        )
    ).all()
    group_rows = (
        await session.execute(
            text("SELECT group_external_id FROM user_groups WHERE user_id = :u"),
            {"u": user_id},
        )
    ).all()

    principals = frozenset(f"{system}:{subject}" for system, subject in identity_rows)
    groups = frozenset(str(row[0]) for row in group_rows)
    return principals, groups
