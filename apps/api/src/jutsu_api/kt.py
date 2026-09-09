"""Knowledge-transfer packages: lifecycle, binding, and the recipient's window.

The security model, stated once and enforced in `_open_for` (§15 of the UI brief):

    KT code  x  recipient identity  x  organisation  x  scope  x  expiry
             x  the recipient's own source permissions / ACL

* **Organisation** — the code lookup runs under RLS, so a foreign tenant's code finds
  nothing and is indistinguishable from a typo. No cross-org probe exists.
* **Recipient identity** — a package bound to an email opens only for the user holding
  that address; an unbound package binds to its FIRST claimer and is a 404 to everyone
  else afterwards. Holding the code proves nothing once it is claimed.
* **Expiry and revocation** — checked server-side on every open. The two sentences the
  UI shows for them come from here, so the frontend cannot soften either.
* **ACL** — nothing in this module grants a document. The documents endpoint joins
  `document_acl` against the RECIPIENT'S own principals inside the SQL, and Ask KT is
  the ordinary `/v1/search` under the recipient's own authorization. A package narrows
  presentation (period, scope); it never widens what its holder could already read.

Denied opens are audited with `outcome = 'denied'` — a stream of refused codes is a
probe, and the trail is where a probe becomes visible.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from jutsu_core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from jutsu_core.ids import ALPHABET, normalise_jutsu_id
from jutsu_db.engine import org_session
from jutsu_retrieval.search import ACL_PREDICATE
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.answers import AnswerOutcome, AnswerTransport, synthesise_answer
from jutsu_api.rate_limit import Bucket, spend_budget

__all__ = [
    "SUPPORTED_SCOPES",
    "KtAdminView",
    "KtRecipientView",
    "claim_or_open",
    "complete_package",
    "create_package",
    "get_package",
    "kt_document",
    "kt_documents",
    "list_packages",
    "open_package_for",
    "revoke_package",
    "update_package",
]

#: The categories the backend can actually serve (§13). Documents come from the corpus
#: under the recipient's ACL; profile from `employee_profiles`; the rest from
#: extraction_claims — evidence-anchored, quote-gated, and filtered by the recipient's
#: own ACL over each claim's evidence at read time.
SUPPORTED_SCOPES: tuple[str, ...] = (
    "documents",
    "profile",
    "decisions",
    "people",
    "projects",
    "meetings",
    "responsibilities",
)

_REVOKED_MESSAGE = "This Knowledge Transfer package has been revoked."
_EXPIRED_MESSAGE = "This Knowledge Transfer package has expired."
#: Finished is not lapsed. Both close the package, and telling somebody who
#: completed their handover that it "expired" reads as a deadline they missed —
#: which is a support conversation about a thing that went right.
_COMPLETED_MESSAGE = (
    "This Knowledge Transfer is complete. Ask your administrator if you need it reopened."
)


def _generate_code() -> str:
    suffix = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"KT-JUTSU-{suffix}"


_NOT_FOUND = "No package matches that ID. Check it with your administrator."


async def _audit(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    action: str,
    resource_id: UUID | str,
    outcome: str = "success",
    correlation_id: str | None = None,
    meta: dict[str, object] | None = None,
) -> None:
    """One KT audit row, on the request session.

    On the request session deliberately: a success row must commit or roll back with the
    change it describes. Denials go through `_audit_denied_open`, which commits on its
    own session for the opposite reason — see its docstring.

    `correlation_id` is the request id (§25), so a row in the trail can be joined to the
    log line that produced it. Never a question, a title, a quote or an address in `meta`
    — the trail is read by administrators and exported; §4.9 has no carve-out for it.
    """
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome, correlation_id, meta_json) "
            "VALUES (:org, :actor, 'user', :action, 'kt_package', :rid, :outcome, "
            ":correlation, cast(:meta AS jsonb))"
        ),
        {
            "org": str(org_id),
            "actor": str(actor_id),
            "action": action,
            "rid": str(resource_id),
            "outcome": outcome,
            "correlation": correlation_id,
            "meta": json.dumps(meta or {}),
        },
    )


async def _touch_activity(session: AsyncSession, *, package_id: UUID) -> None:
    """Record that the recipient did something with the package just now.

    Called from every recipient read, not only from the open — the admin list shows
    `last_activity_at` as "last activity", and a figure that moved only on the shell
    mount understated everything a recipient did after it.
    """
    await session.execute(
        text("UPDATE kt_packages SET last_activity_at = now() WHERE id = :id"),
        {"id": package_id},
    )


# ------------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class KtAdminView:
    id: UUID
    kt_code: str
    subject_user_id: UUID
    subject_name: str | None
    subject_email: str
    #: Derived: active | claimed | expired | revoked | completed. Computed in one place
    #: so the list, the detail and the open path cannot disagree.
    status: str
    scope: list[str]
    period_start: datetime | None
    period_end: datetime | None
    expires_at: datetime
    recipient_email: str | None
    claimed_at: datetime | None
    created_at: datetime
    last_activity_at: datetime | None


@dataclass(frozen=True, slots=True)
class KtAdminPage:
    items: list[KtAdminView]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class SubjectProfile:
    display_name: str | None
    designation: str | None
    department: str | None
    #: The assigned taxonomy, resolved to names. Carried under the SAME `profile` scope
    #: as the two fields above, never as a separate grant: a recipient who may not see
    #: what somebody's job is may not see its normalized grade either.
    #:
    #: It answers the question a handover actually asks — "how senior was the person
    #: whose context I am inheriting, in terms I can compare" — which free-text
    #: `designation` cannot, because every practice spells it differently.
    practice: str | None = None
    role_title: str | None = None
    role_level: str | None = None


@dataclass(frozen=True, slots=True)
class KtRecipientView:
    """What a recipient sees. Deliberately narrower than the admin view: no recipient
    email (they are the recipient), no subject email (display name suffices), and the
    subject's profile only when the package's scope includes it."""

    kt_code: str
    status: str
    scope: list[str]
    period_start: datetime | None
    period_end: datetime | None
    expires_at: datetime
    created_at: datetime
    subject: SubjectProfile


def _derived_status(row: object, *, now: datetime) -> str:
    if getattr(row, "revoked_at", None) is not None:
        return "revoked"
    if getattr(row, "completed_at", None) is not None:
        return "completed"
    if row.expires_at <= now:  # type: ignore[attr-defined]
        return "expired"
    if getattr(row, "recipient_user_id", None) is not None:
        return "claimed"
    return "active"


# ------------------------------------------------------------------------ admin


async def create_package(
    session: AsyncSession,
    *,
    org_id: UUID,
    created_by: UUID,
    subject_user_id: UUID,
    scope: list[str],
    validity_days: int,
    period_days: int | None,
    recipient_email: str | None,
    correlation_id: str | None = None,
) -> KtAdminView:
    """Create a package for one employee's context.

    The subject must exist in this organisation (RLS answers that), the scope must be a
    non-empty subset of what the backend can serve, and validity is bounded: a package
    that never expires is an access decision nobody re-visits.
    """
    cleaned = [category.strip() for category in scope if category.strip()]
    if not cleaned:
        raise ValidationFailed("Choose at least one knowledge category.")
    unsupported = sorted(set(cleaned) - set(SUPPORTED_SCOPES))
    if unsupported:
        raise ValidationFailed(
            f"Not supported yet: {', '.join(unsupported)}. "
            f"Available: {', '.join(SUPPORTED_SCOPES)}."
        )
    if not 1 <= validity_days <= 365:
        raise ValidationFailed("Validity must be between 1 and 365 days.")
    if period_days is not None and not 1 <= period_days <= 3650:
        raise ValidationFailed("The knowledge period must be between 1 day and 10 years.")

    subject = (
        await session.execute(
            text("SELECT id, email FROM users WHERE id = :id"), {"id": subject_user_id}
        )
    ).first()
    if subject is None:
        raise NotFound("That employee was not found.")

    now = datetime.now(tz=UTC)
    package_id = uuid4()
    params: dict[str, object] = {
        "id": package_id,
        "org": str(org_id),
        "subject": subject_user_id,
        "creator": created_by,
        "scope": "[" + ", ".join(f'"{c}"' for c in cleaned) + "]",
        "period_start": now - timedelta(days=period_days) if period_days else None,
        "period_end": now if period_days else None,
        "expires_at": now + timedelta(days=validity_days),
        "recipient": recipient_email.strip().lower() if recipient_email else None,
    }

    # The code is random over a 40-bit space; a collision is unlikely and a retry is
    # cheap. The UNIQUE constraint is what detects it — not a SELECT first, because that
    # read runs under RLS and cannot see another tenant's code, while the constraint is
    # global. Each attempt is a savepoint so a refused INSERT does not abort the request
    # transaction around it. Three attempts, then give up loudly rather than loop.
    for _attempt in range(3):
        params["code"] = _generate_code()
        try:
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO kt_packages (id, org_id, kt_code, subject_user_id, "
                        "created_by, scope, period_start, period_end, expires_at, "
                        "recipient_email) "
                        "VALUES (:id, :org, :code, :subject, :creator, cast(:scope AS jsonb), "
                        ":period_start, :period_end, :expires_at, :recipient)"
                    ),
                    params,
                )
        except IntegrityError:
            continue
        break
    else:  # pragma: no cover - 2^-120 territory, kept for honesty
        raise Conflict("Could not allocate a package code. Try again.")

    await _audit(
        session,
        org_id=org_id,
        actor_id=created_by,
        action="kt.created",
        resource_id=package_id,
        correlation_id=correlation_id,
    )
    return await get_package(session, package_id=package_id)


_ADMIN_SELECT = (
    "SELECT p.id, p.kt_code, p.subject_user_id, p.status, p.scope, p.period_start, "
    "p.period_end, p.expires_at, p.recipient_email, p.recipient_user_id, p.claimed_at, "
    "p.revoked_at, p.completed_at, p.created_at, p.last_activity_at, "
    "u.display_name AS subject_name, u.email AS subject_email, now() AS now "
    "FROM kt_packages p JOIN users u ON u.id = p.subject_user_id "
)


def _admin_view(row: object) -> KtAdminView:
    return KtAdminView(
        id=row.id,  # type: ignore[attr-defined]
        kt_code=row.kt_code,  # type: ignore[attr-defined]
        subject_user_id=row.subject_user_id,  # type: ignore[attr-defined]
        subject_name=row.subject_name,  # type: ignore[attr-defined]
        subject_email=row.subject_email,  # type: ignore[attr-defined]
        status=_derived_status(row, now=row.now),  # type: ignore[attr-defined]
        scope=list(row.scope),  # type: ignore[attr-defined]
        period_start=row.period_start,  # type: ignore[attr-defined]
        period_end=row.period_end,  # type: ignore[attr-defined]
        expires_at=row.expires_at,  # type: ignore[attr-defined]
        recipient_email=row.recipient_email,  # type: ignore[attr-defined]
        claimed_at=row.claimed_at,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        last_activity_at=row.last_activity_at,  # type: ignore[attr-defined]
    )


async def list_packages(session: AsyncSession, *, limit: int, cursor: str | None) -> KtAdminPage:
    bounded = max(1, min(limit, 100))
    filters = ["true"]
    params: dict[str, object] = {"limit": bounded + 1}

    if cursor:
        try:
            ts, last_id = cursor.split("|", 1)
            params["cursor_ts"] = datetime.fromisoformat(ts)
            params["cursor_id"] = UUID(last_id)
        except (ValueError, AttributeError) as exc:
            raise NotFound("That page does not exist.") from exc
        filters.append("(p.created_at, p.id) < (:cursor_ts, :cursor_id)")

    rows = (
        await session.execute(
            text(
                _ADMIN_SELECT
                + f"WHERE {' AND '.join(filters)} "
                + "ORDER BY p.created_at DESC, p.id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    page = rows[:bounded]
    next_cursor = (
        f"{page[-1].created_at.isoformat()}|{page[-1].id}" if len(rows) > bounded and page else None
    )
    return KtAdminPage(items=[_admin_view(r) for r in page], next_cursor=next_cursor)


async def get_package(session: AsyncSession, *, package_id: UUID) -> KtAdminView:
    row = (
        await session.execute(
            text(_ADMIN_SELECT + "WHERE p.id = :id"),
            {"id": package_id},
        )
    ).first()
    if row is None:
        raise NotFound("That package was not found.")
    return _admin_view(row)


async def revoke_package(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    package_id: UUID,
    correlation_id: str | None = None,
) -> KtAdminView:
    """Revocation takes effect at the next authorization check, which is every check."""
    updated = (
        await session.execute(
            text(
                "UPDATE kt_packages SET status = 'revoked', revoked_at = now() "
                "WHERE id = :id AND revoked_at IS NULL RETURNING id"
            ),
            {"id": package_id},
        )
    ).scalar_one_or_none()
    if updated is None:
        # Absent is a 404 from the read; present means it was already revoked. Neither
        # is a success, and a success audit row for a revocation that changed nothing
        # would put a second actor on a transition the first one made.
        view = await get_package(session, package_id=package_id)
        raise Conflict(f"That package is already {view.status}.")
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="kt.revoked",
        resource_id=package_id,
        correlation_id=correlation_id,
    )
    return await get_package(session, package_id=package_id)


async def complete_package(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    package_id: UUID,
    correlation_id: str | None = None,
) -> KtAdminView:
    """Mark a handover finished. Completion also ends access: complete is terminal."""
    updated = (
        await session.execute(
            text(
                "UPDATE kt_packages SET status = 'completed', completed_at = now() "
                "WHERE id = :id AND revoked_at IS NULL AND completed_at IS NULL "
                "RETURNING id"
            ),
            {"id": package_id},
        )
    ).scalar_one_or_none()
    if updated is None:
        # A revoked or already-completed package cannot be completed again. Auditing it
        # as a success anyway would record a transition that never ran.
        view = await get_package(session, package_id=package_id)
        raise Conflict(f"That package is already {view.status}.")
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="kt.completed",
        resource_id=package_id,
        correlation_id=correlation_id,
    )
    return await get_package(session, package_id=package_id)


async def update_package(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    package_id: UUID,
    extend_days: int | None,
    recipient_email: str | None,
    correlation_id: str | None = None,
) -> KtAdminView:
    """Extend a package's expiry, or re-address one nobody has claimed yet.

    Two changes an administrator legitimately needs and could not make: a handover that
    runs long, and a package created for the wrong address before anyone opened it.
    Both are bounded the way creation is. Extension counts from the later of now and the
    current expiry — so a lapsed package can be reopened, which is the point, and a live
    one gains exactly the days asked — and can never place the expiry more than the
    creation ceiling (365 days) past now. Re-addressing is refused once a recipient is
    bound: a claimed package belongs to its claimant, and moving it would be a second
    person's access decided by an edit rather than by a claim.

    A revoked or completed package is terminal and refuses both. Each change is its own
    audit row (`kt.extended` with the before/after expiry; `kt.readdressed` with no
    address — an address is personal data and the trail carries none).
    """
    if extend_days is None and recipient_email is None:
        raise ValidationFailed("Nothing to change.")

    row = (
        await session.execute(
            text(
                "SELECT id, expires_at, revoked_at, completed_at, recipient_user_id, now() AS now "
                "FROM kt_packages WHERE id = :id"
            ),
            {"id": package_id},
        )
    ).first()
    if row is None:
        raise NotFound("That package was not found.")
    if row.revoked_at is not None or row.completed_at is not None:
        view = await get_package(session, package_id=package_id)
        raise Conflict(f"That package is {view.status}; it cannot be changed.")

    if extend_days is not None:
        if not 1 <= extend_days <= 365:
            raise ValidationFailed("An extension must be between 1 and 365 days.")
        base = max(row.now, row.expires_at)
        new_expiry = base + timedelta(days=extend_days)
        if new_expiry > row.now + timedelta(days=365):
            raise ValidationFailed("A package cannot be extended more than a year past today.")
        await session.execute(
            text("UPDATE kt_packages SET expires_at = :expires WHERE id = :id"),
            {"expires": new_expiry, "id": package_id},
        )
        await _audit(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="kt.extended",
            resource_id=package_id,
            correlation_id=correlation_id,
            meta={"expires_at": {"from": row.expires_at.isoformat(), "to": new_expiry.isoformat()}},
        )

    if recipient_email is not None:
        if row.recipient_user_id is not None:
            raise Conflict("That package is already claimed; its recipient cannot change.")
        await session.execute(
            text("UPDATE kt_packages SET recipient_email = :email WHERE id = :id"),
            {"email": recipient_email.strip().lower(), "id": package_id},
        )
        await _audit(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="kt.readdressed",
            resource_id=package_id,
            correlation_id=correlation_id,
        )

    return await get_package(session, package_id=package_id)


# ---------------------------------------------------------------------- recipient


logger = logging.getLogger("jutsu.api.kt")

#: Why an open was refused. The CALLER never learns which — a KT ID must confirm
#: nothing to whoever holds it, so all of these answer 404 or the one §39 sentence.
#: Recorded server-side because the uniform refusal that makes the code space safe is
#: also what makes a real support case ("B cannot open A's package") undiagnosable:
#: unknown code, bound elsewhere and addressed elsewhere are one response and three
#: completely different fixes.
DENIED_UNKNOWN_CODE = "unknown_code"
DENIED_BOUND_TO_ANOTHER = "bound_to_another_user"
DENIED_ADDRESSED_TO_ANOTHER = "addressed_to_another_email"
DENIED_REVOKED = "revoked"
DENIED_COMPLETED = "completed"
DENIED_EXPIRED = "expired"
DENIED_UNCLAIMED_ON_READ = "unclaimed_via_read_route"
DENIED_CLAIM_RACE_LOST = "claim_race_lost"


async def _audit_denied_open(
    *, org_id: UUID, actor_id: UUID, resource_id: str, reason: str
) -> None:
    """A denied open, committed so it outlives the refusal that follows it.

    Every caller raises immediately after this, and `get_db` rolls the request
    transaction back with the exception — a denial written on the request session
    recorded nothing, and the probe trail the module docstring promises never existed.
    So the row goes on its own org-scoped session and commits before the refusal
    unwinds, the same shape as the search limiter's spend (rate_limit.py). The claimed
    *success* row stays on the request session deliberately: it must commit or roll
    back with the claim it describes.
    """
    # `outcome` is `success | denied | failure` and nothing else — a reason written
    # there fails the CHECK constraint from migration 0002, so it goes in `meta_json`.
    async with org_session(org_id) as audit:
        await audit.execute(
            text(
                "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
                "resource_id, outcome, meta_json) "
                "VALUES (:org, :actor, 'user', 'kt.open', 'kt_package', :rid, 'denied', "
                "cast(:meta AS jsonb))"
            ),
            {
                "org": str(org_id),
                "actor": str(actor_id),
                "rid": resource_id,
                "meta": json.dumps({"reason": reason}),
            },
        )
    # Deliberately not the code: it is a capability, and §4.9 admits no exception for
    # a log line. `request_id`, `org_id` and the opaque `user_id` are already bound to
    # every record by `RequestContextFilter`, which is what joins this to the request.
    logger.info("%s", {"event": "kt_open_denied", "reason": reason})


async def _open_for(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    budgeted: bool = True,
    may_claim: bool = False,
) -> object:
    """The one authorization path for recipients. Everything KT-scoped calls this.

    Refusals in order: unknown/foreign/typo'd code (404, all identical), revoked (403,
    the exact sentence §39 requires), expired (403), wrong person (404 — a bound
    package must not confirm its own existence to the wrong holder).

    **Every code that arrives here costs a `KT_OPEN` allowance, spent before the
    lookup.** The claim wall was written to turn a 32^8 code space into something no
    probe finishes, but it was spent in exactly one place — `POST /v1/kt/claim` —
    while a dozen sibling routes reach this same lookup with a caller-supplied code
    and were unbudgeted, so guessing against `GET /v1/kt/{code}/documents` was free.

    Two details are load-bearing. It is a *different* bucket from the claim door,
    because a person reading a package makes several requests per panel and would
    spend the claim allowance in seconds. And it is charged BEFORE the lookup, so a
    hit and a miss cost the same — a budget spent only on misses would answer, once
    exhausted, the exact question the 404 is written to refuse.

    `budgeted=False` is for `claim_or_open` alone: that route already spends
    `KT_CLAIM` before calling in, and charging twice for one attempt halves the
    stated allowance.

    **`may_claim` is why binding is not a side effect of reading.** Claiming is an
    irreversible state change — it decides, permanently, whose package this is — and it
    used to happen inside every one of the nine GET routes that reach here.
    `verify_csrf` returns without checking anything on a GET (its own docstring says Lax
    "is not sufficient on its own for anything that changes state via a link"), and
    `SameSite=Lax` still sends the session cookie on a top-level navigation. So a link to
    `/v1/kt/{code}/documents`, clicked by a colleague, permanently bound an unaddressed
    package to whoever clicked — no form, no POST, no token.

    Only `claim_or_open` passes `may_claim=True`, and it is reached exclusively through
    `POST /v1/kt/claim`, which is CSRF-checked and `KT_CLAIM`-budgeted. Every other
    caller now gets the ordinary 404 for an unbound package, which is also what the
    console already produces: `KtShell` claims by POST on mount before any panel loads.
    """
    if budgeted:
        await spend_budget(Bucket.KT_OPEN, org_id=org_id, user_id=user_id)
    code = normalise_jutsu_id(kt_code)

    lookup = text(
        "SELECT p.*, u.email AS caller_email, now() AS now FROM kt_packages p, "
        "users u WHERE p.kt_code = :code AND u.id = :user"
    )
    row = (await session.execute(lookup, {"code": code, "user": user_id})).first()
    if row is None:
        await _audit_denied_open(
            org_id=org_id,
            actor_id=user_id,
            resource_id=code[:64],
            reason=DENIED_UNKNOWN_CODE,
        )
        raise NotFound(_NOT_FOUND)

    # Binding BEFORE state. A package that belongs to somebody else is a 404 whatever
    # its state: answering "revoked" or "expired" to the wrong holder confirms both that
    # the package exists and that it was closed, and a KT ID must confirm nothing to
    # whoever happens to hold it. The exact revoked/expired sentences are still what the
    # RIGHT person sees (§39) — and what any member sees for a package that was never
    # addressed to anyone, since there is no one for it to be kept from.
    bound_elsewhere = row.recipient_user_id is not None and row.recipient_user_id != user_id
    addressed_elsewhere = (
        row.recipient_user_id is None
        and row.recipient_email is not None
        and row.recipient_email != row.caller_email.lower()
    )
    if bound_elsewhere or addressed_elsewhere:
        await _audit_denied_open(
            org_id=org_id,
            actor_id=user_id,
            resource_id=str(row.id),
            reason=DENIED_BOUND_TO_ANOTHER if bound_elsewhere else DENIED_ADDRESSED_TO_ANOTHER,
        )
        raise NotFound(_NOT_FOUND)

    if row.revoked_at is not None:
        await _audit_denied_open(
            org_id=org_id, actor_id=user_id, resource_id=str(row.id), reason=DENIED_REVOKED
        )
        raise PermissionDenied(_REVOKED_MESSAGE)
    if row.completed_at is not None:
        await _audit_denied_open(
            org_id=org_id, actor_id=user_id, resource_id=str(row.id), reason=DENIED_COMPLETED
        )
        raise PermissionDenied(_COMPLETED_MESSAGE)
    if row.expires_at <= row.now:
        await _audit_denied_open(
            org_id=org_id, actor_id=user_id, resource_id=str(row.id), reason=DENIED_EXPIRED
        )
        raise PermissionDenied(_EXPIRED_MESSAGE)

    if row.recipient_user_id is not None:
        # Bound to this caller. The row still carries recipient_user_id, which is how
        # `claim_or_open` tells a re-open from the first claim below.
        return row

    if not may_claim:
        # Unbound, and this caller came through a route that may not bind. Reading must
        # never decide whose package this is — see `may_claim` in the docstring. The same
        # 404 as an unknown code, because "exists but you have not claimed it" is exactly
        # the fact a KT ID must not confirm to whoever happens to hold it.
        await _audit_denied_open(
            org_id=org_id,
            actor_id=user_id,
            resource_id=str(row.id),
            reason=DENIED_UNCLAIMED_ON_READ,
        )
        raise NotFound(_NOT_FOUND)

    # First eligible opener claims it. From here on, everyone else is a 404. The
    # rowcount is the race detector: two concurrent first opens both read the package
    # unbound, but only one UPDATE binds — and the loser must get the same refusal a
    # wrong recipient gets, not the contents plus a success row for a claim that never
    # happened.
    claimed = (
        await session.execute(
            text(
                "UPDATE kt_packages SET recipient_user_id = :user, claimed_at = now() "
                "WHERE id = :id AND recipient_user_id IS NULL RETURNING id"
            ),
            {"user": user_id, "id": row.id},
        )
    ).scalar_one_or_none()
    if claimed is None:
        row = (await session.execute(lookup, {"code": code, "user": user_id})).first()
        if row is None or row.recipient_user_id != user_id:
            await _audit_denied_open(
                org_id=org_id,
                actor_id=user_id,
                resource_id=code[:64],
                reason=DENIED_CLAIM_RACE_LOST,
            )
            raise NotFound("No package matches that ID. Check it with your administrator.")
        # The same caller won through a parallel request; that request wrote the
        # success row, so this one records nothing twice.
        return row

    # On the request session deliberately: the success row must commit or roll back
    # with the binding it describes, unlike the denials above.
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', 'kt.claimed', 'kt_package', :rid, 'success')"
        ),
        {"org": str(org_id), "actor": str(user_id), "rid": str(row.id)},
    )
    return row


async def open_package_for(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, kt_code: str
) -> UUID:
    """`_open_for`, for a sibling module, returning only the package id.

    Exported so that a module handling KT-scoped resources reaches the package through
    THIS path rather than writing its own `SELECT ... FROM kt_packages` — which is how a
    second authorization path gets born. `kt_files` is the first such caller (ADR 0021).

    **The id and nothing else, deliberately.** A caller outside this module wants to join
    on the package, not to render it; everything a recipient may know about a package is
    already shaped by `KtRecipientView`. Handing back the row would let the next caller
    read `subject_user_id` or `recipient_email` straight out of it and put either on a
    screen that was never checked for them.
    """
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    return UUID(str(row.id))  # type: ignore[attr-defined]


async def claim_or_open(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    correlation_id: str | None = None,
) -> KtRecipientView:
    """Open (claiming if unclaimed) and return the recipient's view of the package.

    Every open is an access event (§25 `KT_ACCESSED`). The first one is already written
    as `kt.claimed` inside `_open_for`, on the same session as the binding it describes;
    every later one is `kt.opened` here. The row `_open_for` returns tells the two apart:
    the first-claim path returns the row as it was read, before the UPDATE bound it, so
    `recipient_user_id` is still NULL exactly when this call performed the claim.
    """
    # `POST /v1/kt/claim` spends `KT_CLAIM` before it calls in, so the open is not
    # charged again: one attempt, one charge. Every other route reaches `_open_for`
    # directly and pays the `KT_OPEN` allowance there.
    # The one caller allowed to bind: this is `POST /v1/kt/claim`, which is CSRF-checked
    # and already spent `KT_CLAIM`.
    row = await _open_for(
        session,
        org_id=org_id,
        user_id=user_id,
        kt_code=kt_code,
        budgeted=False,
        may_claim=True,
    )

    await _touch_activity(session, package_id=row.id)  # type: ignore[attr-defined]
    if row.recipient_user_id is not None:  # type: ignore[attr-defined]
        await _audit(
            session,
            org_id=org_id,
            actor_id=user_id,
            action="kt.opened",
            resource_id=row.id,  # type: ignore[attr-defined]
            correlation_id=correlation_id,
        )

    scope = list(row.scope)  # type: ignore[attr-defined]
    profile_row = None
    if "profile" in scope:
        profile_row = (
            await session.execute(
                text(
                    "SELECT u.display_name, ep.designation, ep.department, "
                    "  p.display_name AS practice, "
                    "  COALESCE(t.display_name, ep.role_title_custom) AS role_title, "
                    "  l.display_name AS role_level "
                    "FROM users u "
                    "LEFT JOIN employee_profiles ep ON ep.user_id = u.id "
                    "LEFT JOIN role_practices p ON p.key = ep.practice_key "
                    "LEFT JOIN role_titles t ON t.key = ep.role_title_key "
                    "LEFT JOIN role_levels l ON l.key = ep.role_level_key "
                    "WHERE u.id = :subject"
                ),
                {"subject": row.subject_user_id},  # type: ignore[attr-defined]
            )
        ).first()
    else:
        profile_row = (
            await session.execute(
                text(
                    "SELECT display_name, NULL AS designation, NULL AS department, "
                    "  NULL AS practice, NULL AS role_title, NULL AS role_level "
                    "FROM users WHERE id = :subject"
                ),
                {"subject": row.subject_user_id},  # type: ignore[attr-defined]
            )
        ).first()

    return KtRecipientView(
        kt_code=row.kt_code,  # type: ignore[attr-defined]
        status="claimed",
        scope=scope,
        period_start=row.period_start,  # type: ignore[attr-defined]
        period_end=row.period_end,  # type: ignore[attr-defined]
        expires_at=row.expires_at,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        subject=SubjectProfile(
            display_name=profile_row.display_name if profile_row else None,
            designation=profile_row.designation if profile_row else None,
            department=profile_row.department if profile_row else None,
            practice=profile_row.practice if profile_row else None,
            role_title=profile_row.role_title if profile_row else None,
            role_level=profile_row.role_level if profile_row else None,
        ),
    )


@dataclass(frozen=True, slots=True)
class KtDocument:
    id: UUID
    title: str
    source_system: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class KtDocumentPage:
    items: list[KtDocument]
    next_cursor: str | None


async def kt_documents(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    principals: frozenset[str],
    groups: frozenset[str],
    limit: int,
    cursor: str | None,
) -> KtDocumentPage:
    """Documents in the package window THE RECIPIENT MAY ALREADY READ.

    The ACL join is inside the SQL, against the caller's own principals — the same rule
    as retrieval (§12, non-negotiable 5). The package contributes only the period
    filter. A recipient with no linked source identity gets an empty page, which is the
    §2 invariant holding, not a bug; the UI explains it in exactly those terms.
    """
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    if "documents" not in list(row.scope):  # type: ignore[attr-defined]
        raise PermissionDenied("Documents are not part of this package's scope.")
    await _touch_activity(session, package_id=row.id)  # type: ignore[attr-defined]

    # No early return on empty principals: the predicate's third arm serves documents
    # granted to the whole organisation, which a caller with no personal principal may
    # still read. Empty arrays simply make the first two arms false.
    bounded = max(1, min(limit, 100))
    filters = ["d.superseded_by IS NULL"]
    params: dict[str, object] = {
        "limit": bounded + 1,
        "principals": list(principals),
        "groups": list(groups),
    }

    if row.period_start is not None:  # type: ignore[attr-defined]
        params["period_start"] = row.period_start  # type: ignore[attr-defined]
        filters.append("d.created_at >= :period_start")
    if row.period_end is not None:  # type: ignore[attr-defined]
        params["period_end"] = row.period_end  # type: ignore[attr-defined]
        filters.append("d.created_at <= :period_end")
    if cursor:
        try:
            ts, last_id = cursor.split("|", 1)
            params["cursor_ts"] = datetime.fromisoformat(ts)
            params["cursor_id"] = UUID(last_id)
        except (ValueError, AttributeError) as exc:
            raise NotFound("That page does not exist.") from exc
        filters.append("(d.created_at, d.id) < (:cursor_ts, :cursor_id)")

    # THE predicate, imported from retrieval rather than re-derived: §12's rule is that
    # the same authorization filter runs everywhere, and a hand-written near-copy here
    # is exactly how a KT listing would quietly widen (or narrow) what search enforces.
    rows = (
        await session.execute(
            text(
                "SELECT d.id, d.title, d.created_at, "  # noqa: S608
                "s.system AS source_system "
                "FROM documents d "
                "JOIN sources s ON s.id = d.source_id "
                f"WHERE {ACL_PREDICATE} "
                f"AND {' AND '.join(filters)} "
                "ORDER BY d.created_at DESC, d.id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    page = rows[:bounded]
    next_cursor = (
        f"{page[-1].created_at.isoformat()}|{page[-1].id}" if len(rows) > bounded and page else None
    )
    return KtDocumentPage(
        items=[
            KtDocument(
                id=r.id,
                title=r.title,
                source_system=r.source_system,
                created_at=r.created_at,
            )
            for r in page
        ],
        next_cursor=next_cursor,
    )


@dataclass(frozen=True, slots=True)
class KtDocumentChunk:
    """One passage of a document, in the form the platform stores it: MASKED text.

    No `char_start` / `char_end`, for the same reason `HandoverEvidence` carries no span.
    The stored offsets index the ORIGINAL body while this `text` is the masked one, so
    shipping the pair together is precisely the mis-highlight trap ADR 0005 records — the
    numbers would look applicable to the string beside them and land somewhere else. A
    reader needs the words; a citation span comes from `/v1/evidence/{chunk_id}`, which
    re-checks the caller's ACL itself and returns offsets against text they match.
    """

    ordinal: int
    text: str


@dataclass(frozen=True, slots=True)
class KtDocumentDetail:
    id: UUID
    title: str
    source_system: str
    created_at: datetime
    chunks: list[KtDocumentChunk]
    total_chunks: int
    #: The ordinal to ask for next, or None at the end of the document. An ordinal rather
    #: than an opaque cursor because it is already the document's own total order and a
    #: reader legitimately wants to say "showing 1-50 of 214".
    next_ordinal: int | None


#: One sentence for three different facts: no such document, not one this caller may read,
#: and one outside the package's period. Telling them apart would turn the endpoint into a
#: probe for what the tenant holds and where the package's window ends — the same reasoning
#: that makes an unknown KT code and a package bound to somebody else the same 404.
_DOCUMENT_NOT_FOUND = "That document is not available in this package."


async def kt_document(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    document_id: UUID,
    principals: frozenset[str],
    groups: frozenset[str],
    from_ordinal: int,
    limit: int,
) -> KtDocumentDetail:
    """One document from the package window, as ordered MASKED passages.

    `kt_documents` is a bibliography: it proves a document exists and is authorised to the
    recipient, and lets them read not a word of it. This is the same window, opened.

    Every gate the listing runs, in the same order and from the same constant: `_open_for`
    first, then the package's scope, then the recipient's own ACL and the package's period
    — the last two ANDed together **inside** the SQL, so one statement decides both. The
    passage read re-runs that whole condition rather than inheriting the header's verdict;
    two statements that could disagree about authorization is one more than there should be.

    Paginated by ordinal because a document is not bounded: an ingested handbook is
    hundreds of chunks, and returning all of them makes one response megabytes wide.
    """
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    if "documents" not in list(row.scope):  # type: ignore[attr-defined]
        raise PermissionDenied("Documents are not part of this package's scope.")
    await _touch_activity(session, package_id=row.id)  # type: ignore[attr-defined]

    params: dict[str, object] = {
        "id": document_id,
        "principals": list(principals),
        "groups": list(groups),
    }
    # The window, as conjuncts on `d`. It sits beside ACL_PREDICATE and is ANDed with it,
    # never applied to a wider result afterwards: intersection can only remove a document
    # the caller was already authorized to see, and can never add one.
    window = ["d.superseded_by IS NULL"]
    if row.period_start is not None:  # type: ignore[attr-defined]
        params["period_start"] = row.period_start  # type: ignore[attr-defined]
        window.append("d.created_at >= :period_start")
    if row.period_end is not None:  # type: ignore[attr-defined]
        params["period_end"] = row.period_end  # type: ignore[attr-defined]
        window.append("d.created_at <= :period_end")

    header = (
        await session.execute(
            text(
                "SELECT d.id, d.title, d.created_at, "  # noqa: S608
                "CAST(s.system AS text) AS source_system, "
                "(SELECT count(*) FROM chunks c WHERE c.document_id = d.id) AS total_chunks "
                "FROM documents d "
                "JOIN sources s ON s.id = d.source_id "
                f"WHERE {ACL_PREDICATE} AND d.id = :id AND {' AND '.join(window)}"
            ),
            params,
        )
    ).first()
    if header is None:
        raise NotFound(_DOCUMENT_NOT_FOUND)

    bounded = max(1, min(limit, 200))
    params["from_ordinal"] = max(0, from_ordinal)
    params["limit"] = bounded + 1
    rows = (
        await session.execute(
            text(
                "SELECT c.ordinal, c.text FROM chunks c "  # noqa: S608
                "WHERE c.document_id = :id AND c.ordinal >= :from_ordinal "
                "AND EXISTS (SELECT 1 FROM documents d WHERE d.id = c.document_id "
                f"AND {' AND '.join(window)} AND {ACL_PREDICATE}) "
                "ORDER BY c.ordinal LIMIT :limit"
            ),
            params,
        )
    ).all()

    page = rows[:bounded]
    # `+ 1` rather than the last ordinal: the next request asks for ordinals `>= n`, and
    # handing back the one just read would repeat a passage on every page boundary.
    next_ordinal = page[-1].ordinal + 1 if len(rows) > bounded and page else None
    return KtDocumentDetail(
        id=header.id,
        title=header.title,
        source_system=header.source_system,
        created_at=header.created_at,
        chunks=[KtDocumentChunk(ordinal=r.ordinal, text=r.text) for r in page],
        total_chunks=header.total_chunks,
        next_ordinal=next_ordinal,
    )


# ------------------------------------------------------------------------ insights

#: Which package scope category authorises which claim type. The wizard's categories
#: and the extractor's taxonomy meet here, in one place.
_CLAIM_SCOPE: dict[str, str] = {
    "decision": "decisions",
    "person": "people",
    "project": "projects",
    "meeting": "meetings",
    "responsibility": "responsibilities",
}


@dataclass(frozen=True, slots=True)
class KtInsight:
    id: UUID
    claim_type: str
    summary: str | None
    name: str | None
    date: str | None
    quote: str
    confidence: float
    document_id: UUID
    document_title: str
    source_system: str
    chunk_id: UUID
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class KtInsightSummary:
    by_type: dict[str, int]


_LATEST_RUN_JOIN = (
    "JOIN extraction_runs r ON r.id = cl.run_id AND r.finished_at IS NOT NULL "
    "AND r.id = ("
    "  SELECT r2.id FROM extraction_runs r2 "
    "  WHERE r2.stats_json->>'document_id' = d.id::text "
    "  AND r2.finished_at IS NOT NULL "
    "  ORDER BY r2.started_at DESC LIMIT 1"
    ") "
)


async def kt_insights(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    principals: frozenset[str],
    groups: frozenset[str],
    claim_type: str | None,
    limit: int,
) -> list[KtInsight]:
    """Extracted claims inside the package window THE RECIPIENT MAY ALREADY READ.

    Three gates, in the order they run: the package itself (`_open_for` — binding,
    expiry, revocation), the package's scope (a claim type outside it is refused), and
    the recipient's own ACL — retrieval's predicate, inside the SQL, over the DOCUMENT
    each claim's evidence chunk belongs to. A claim whose evidence the caller cannot
    read does not exist for them (non-negotiable 6).

    Only claims from each document's LATEST finished run qualify: re-extraction
    supersedes by versioning, and the read model is where "current" is defined.
    """
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    scope = list(row.scope)  # type: ignore[attr-defined]
    await _touch_activity(session, package_id=row.id)  # type: ignore[attr-defined]

    if claim_type is not None:
        category = _CLAIM_SCOPE.get(claim_type)
        if category is None:
            raise ValidationFailed(f"Unknown insight type. One of: {', '.join(_CLAIM_SCOPE)}.")
        if category not in scope:
            raise PermissionDenied(f"{category.capitalize()} are not part of this package's scope.")

    bounded = max(1, min(limit, 200))
    filters = ["d.superseded_by IS NULL"]
    params: dict[str, object] = {
        "limit": bounded,
        "principals": list(principals),
        "groups": list(groups),
    }
    if claim_type is not None:
        params["claim_type"] = claim_type
        filters.append("cl.claim_type = :claim_type")
    else:
        # The timeline: every type the package's scope covers.
        allowed = [t for t, cat in _CLAIM_SCOPE.items() if cat in scope]
        if not allowed:
            return []
        params["allowed_types"] = allowed
        filters.append("cl.claim_type = ANY(:allowed_types)")
    if row.period_start is not None:  # type: ignore[attr-defined]
        params["period_start"] = row.period_start  # type: ignore[attr-defined]
        filters.append("d.created_at >= :period_start")
    if row.period_end is not None:  # type: ignore[attr-defined]
        params["period_end"] = row.period_end  # type: ignore[attr-defined]
        filters.append("d.created_at <= :period_end")

    rows = (
        await session.execute(
            text(
                "SELECT cl.id, cl.claim_type, cl.confidence, cl.payload_json, "
                "cl.chunk_id, "
                "d.id AS document_id, d.title AS document_title, "
                "CAST(s.system AS text) AS source_system, d.created_at AS occurred_at "
                "FROM extraction_claims cl "
                "JOIN chunks ch ON ch.id = cl.chunk_id "
                "JOIN documents d ON d.id = ch.document_id "
                "JOIN sources s ON s.id = d.source_id "
                + _LATEST_RUN_JOIN
                + f"WHERE {ACL_PREDICATE} AND {' AND '.join(filters)} "
                "ORDER BY COALESCE(NULLIF(cl.payload_json->>'date', ''), "
                "to_char(d.created_at, 'YYYY-MM-DD')) DESC, cl.id DESC "
                "LIMIT :limit"
            ),
            params,
        )
    ).all()

    return [
        KtInsight(
            id=r.id,
            claim_type=r.claim_type,
            summary=(r.payload_json.get("summary") or None),
            name=(r.payload_json.get("name") or None),
            date=(r.payload_json.get("date") or None),
            quote=r.payload_json.get("quote", ""),
            confidence=r.confidence,
            document_id=r.document_id,
            document_title=r.document_title,
            source_system=r.source_system,
            chunk_id=r.chunk_id,
            occurred_at=r.occurred_at,
        )
        for r in rows
    ]


async def kt_insight_summary(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    principals: frozenset[str],
    groups: frozenset[str],
) -> KtInsightSummary:
    """Counts per claim type, under exactly the gates the lists themselves run.

    This is where the Overview's and the Handover's figures come from — the same ACL
    predicate that will serve the rows, so a count can never exceed what its list would
    show (§17.6 in miniature).
    """
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    scope = list(row.scope)  # type: ignore[attr-defined]
    await _touch_activity(session, package_id=row.id)  # type: ignore[attr-defined]
    allowed = [t for t, cat in _CLAIM_SCOPE.items() if cat in scope]
    if not allowed:
        return KtInsightSummary(by_type={})

    filters = ["d.superseded_by IS NULL", "cl.claim_type = ANY(:allowed_types)"]
    params: dict[str, object] = {
        "principals": list(principals),
        "groups": list(groups),
        "allowed_types": allowed,
    }
    if row.period_start is not None:  # type: ignore[attr-defined]
        params["period_start"] = row.period_start  # type: ignore[attr-defined]
        filters.append("d.created_at >= :period_start")
    if row.period_end is not None:  # type: ignore[attr-defined]
        params["period_end"] = row.period_end  # type: ignore[attr-defined]
        filters.append("d.created_at <= :period_end")

    rows = (
        await session.execute(
            text(
                "SELECT cl.claim_type, count(*) AS n "
                "FROM extraction_claims cl "
                "JOIN chunks ch ON ch.id = cl.chunk_id "
                "JOIN documents d ON d.id = ch.document_id "
                + _LATEST_RUN_JOIN
                + f"WHERE {ACL_PREDICATE} AND {' AND '.join(filters)} "
                "GROUP BY cl.claim_type"
            ),
            params,
        )
    ).all()
    return KtInsightSummary(by_type={r.claim_type: r.n for r in rows})


@dataclass(frozen=True, slots=True)
class HandoverEvidence:
    """A claim shaped for the answer synthesiser's `Groundable` protocol.

    No char offsets, deliberately: the claim's offsets index the chunk's MASKED text
    and the retrieval `Evidence` contract promises original-body offsets — reusing the
    field would plant exactly the mis-highlight trap CLAUDE.md warns about. A handover
    citation points at a document, not a span, and says so by carrying no span.
    """

    chunk_id: UUID
    document_id: UUID
    document_title: str
    source_system: str
    text: str


_HANDOVER_QUESTION = (
    "Compose a concise executive handover summary for the person taking over: main "
    "responsibilities, active projects, key contacts, important decisions, and open "
    "work. Group related points; write for a first day on the job."
)


async def kt_handover_summary(
    session: AsyncSession,
    transport: AnswerTransport,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    principals: frozenset[str],
    groups: frozenset[str],
) -> AnswerOutcome:
    """§29's executive summary, composed from evidence-anchored claims and gated.

    The same grounding discipline as /v1/ask: the model sees only claims the recipient
    may already read (kt_insights runs all three gates), every sentence must cite, the
    citations are validated against exactly that claim list, and an unciteable summary
    is an honest `insufficient_evidence` — never a fluent guess (non-negotiable 3).
    Composed on demand and never persisted: a stored summary would outlive the ACL
    state it was grounded in.
    """
    insights = await kt_insights(
        session,
        org_id=org_id,
        user_id=user_id,
        kt_code=kt_code,
        principals=principals,
        groups=groups,
        claim_type=None,
        limit=40,
    )
    evidence = [
        HandoverEvidence(
            chunk_id=i.chunk_id,
            document_id=i.document_id,
            document_title=i.document_title,
            source_system=i.source_system,
            text=(
                f"{i.claim_type}"
                + (f" — {i.name}" if i.name else "")
                + (f": {i.summary}" if i.summary else "")
                + f'\nEvidence: "{i.quote}"'
                + (f"\nDate: {i.date}" if i.date else "")
            ),
        )
        for i in insights
    ]
    outcome = await synthesise_answer(transport, question=_HANDOVER_QUESTION, evidence=evidence)
    # §25 names this act, and it is the one recipient-facing surface that spends a model
    # call and composes a narrative over somebody else's documents. The row says it
    # happened and how it went; the summary itself is never written down, here or
    # anywhere — a stored one would outlive the ACL state that grounded it.
    # The package id, not the code. Every other kt_package audit row keys on the UUID,
    # and the admin console's activity panel filters on exactly that — so keying this
    # one differently made the single act that spends a model call over somebody else's
    # documents the one act a package's own trail never showed. `_open_for` has already
    # authorised this caller, so the lookup is scoped and cannot widen anything.
    package_id = (
        await session.execute(
            text("SELECT id FROM kt_packages WHERE kt_code = :code"),
            {"code": normalise_jutsu_id(kt_code)},
        )
    ).scalar_one_or_none()
    await _audit(
        session,
        org_id=org_id,
        actor_id=user_id,
        action="kt.handover_summary",
        resource_id=package_id if package_id is not None else normalise_jutsu_id(kt_code),
        outcome="success",
        meta={
            "insufficient_evidence": outcome.insufficient_evidence,
            "citations": len(outcome.citations),
            "attempts": outcome.attempts,
            "claims_considered": len(insights),
        },
    )
    return outcome
