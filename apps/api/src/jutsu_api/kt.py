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

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from jutsu_core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from jutsu_core.ids import ALPHABET, normalise_jutsu_id
from jutsu_retrieval.search import ACL_PREDICATE
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "SUPPORTED_SCOPES",
    "KtAdminView",
    "KtRecipientView",
    "claim_or_open",
    "complete_package",
    "create_package",
    "get_package",
    "kt_documents",
    "list_packages",
    "revoke_package",
]

#: The categories the backend can actually serve today, and no others (§13: "do not
#: expose categories that the backend cannot support"). Documents come from the corpus
#: under the recipient's ACL; profile comes from `employee_profiles`. Projects,
#: decisions, meetings, people and timeline arrive with knowledge-graph extraction and
#: join this tuple when they do.
SUPPORTED_SCOPES: tuple[str, ...] = ("documents", "profile")

_REVOKED_MESSAGE = "This Knowledge Transfer package has been revoked."
_EXPIRED_MESSAGE = "This Knowledge Transfer package has expired."


def _generate_code() -> str:
    suffix = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"KT-JUTSU-{suffix}"


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

    # The code is random over a 40-bit space; a collision is unlikely and a retry is
    # cheap. Three attempts, then give up loudly rather than loop forever.
    for _attempt in range(3):
        code = _generate_code()
        exists = (
            await session.execute(
                text("SELECT 1 FROM kt_packages WHERE kt_code = :code"), {"code": code}
            )
        ).scalar_one_or_none()
        if exists is None:
            break
    else:  # pragma: no cover - 2^-120 territory, kept for honesty
        raise Conflict("Could not allocate a package code. Try again.")

    await session.execute(
        text(
            "INSERT INTO kt_packages (id, org_id, kt_code, subject_user_id, created_by, "
            "scope, period_start, period_end, expires_at, recipient_email) "
            "VALUES (:id, :org, :code, :subject, :creator, cast(:scope AS jsonb), "
            ":period_start, :period_end, :expires_at, :recipient)"
        ),
        {
            "id": package_id,
            "org": str(org_id),
            "code": code,
            "subject": subject_user_id,
            "creator": created_by,
            "scope": "[" + ", ".join(f'"{c}"' for c in cleaned) + "]",
            "period_start": now - timedelta(days=period_days) if period_days else None,
            "period_end": now if period_days else None,
            "expires_at": now + timedelta(days=validity_days),
            "recipient": recipient_email.strip().lower() if recipient_email else None,
        },
    )
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', 'kt.created', 'kt_package', :rid, 'success')"
        ),
        {"org": str(org_id), "actor": str(created_by), "rid": str(package_id)},
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
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, package_id: UUID
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
        # Either absent or already revoked; the detail read below answers which.
        pass
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', 'kt.revoked', 'kt_package', :rid, 'success')"
        ),
        {"org": str(org_id), "actor": str(actor_id), "rid": str(package_id)},
    )
    return await get_package(session, package_id=package_id)


async def complete_package(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, package_id: UUID
) -> KtAdminView:
    """Mark a handover finished. Completion also ends access: complete is terminal."""
    await session.execute(
        text(
            "UPDATE kt_packages SET status = 'completed', completed_at = now() "
            "WHERE id = :id AND revoked_at IS NULL AND completed_at IS NULL"
        ),
        {"id": package_id},
    )
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', 'kt.completed', 'kt_package', :rid, 'success')"
        ),
        {"org": str(org_id), "actor": str(actor_id), "rid": str(package_id)},
    )
    return await get_package(session, package_id=package_id)


# ---------------------------------------------------------------------- recipient


async def _audit_open(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    action: str,
    resource_id: str,
    outcome: str,
) -> None:
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', :action, 'kt_package', :rid, :outcome)"
        ),
        {
            "org": str(org_id),
            "actor": str(actor_id),
            "action": action,
            "rid": resource_id,
            "outcome": outcome,
        },
    )


async def _open_for(session: AsyncSession, *, org_id: UUID, user_id: UUID, kt_code: str) -> object:
    """The one authorization path for recipients. Everything KT-scoped calls this.

    Refusals in order: unknown/foreign/typo'd code (404, all identical), revoked (403,
    the exact sentence §39 requires), expired (403), wrong person (404 — a bound
    package must not confirm its own existence to the wrong holder).
    """
    code = normalise_jutsu_id(kt_code)

    row = (
        await session.execute(
            text(
                "SELECT p.*, u.email AS caller_email, now() AS now FROM kt_packages p, "
                "users u WHERE p.kt_code = :code AND u.id = :user"
            ),
            {"code": code, "user": user_id},
        )
    ).first()
    if row is None:
        await _audit_open(
            session,
            org_id=org_id,
            actor_id=user_id,
            action="kt.open",
            resource_id=code[:64],
            outcome="denied",
        )
        raise NotFound("No package matches that ID. Check it with your administrator.")

    if row.revoked_at is not None:
        await _audit_open(
            session,
            org_id=org_id,
            actor_id=user_id,
            action="kt.open",
            resource_id=str(row.id),
            outcome="denied",
        )
        raise PermissionDenied(_REVOKED_MESSAGE)
    if row.completed_at is not None or row.expires_at <= row.now:
        await _audit_open(
            session,
            org_id=org_id,
            actor_id=user_id,
            action="kt.open",
            resource_id=str(row.id),
            outcome="denied",
        )
        raise PermissionDenied(_EXPIRED_MESSAGE)

    if row.recipient_user_id is not None:
        if row.recipient_user_id != user_id:
            await _audit_open(
                session,
                org_id=org_id,
                actor_id=user_id,
                action="kt.open",
                resource_id=str(row.id),
                outcome="denied",
            )
            raise NotFound("No package matches that ID. Check it with your administrator.")
        return row

    if row.recipient_email is not None and row.recipient_email != row.caller_email.lower():
        await _audit_open(
            session,
            org_id=org_id,
            actor_id=user_id,
            action="kt.open",
            resource_id=str(row.id),
            outcome="denied",
        )
        raise NotFound("No package matches that ID. Check it with your administrator.")

    # First eligible opener claims it. From here on, everyone else is a 404.
    await session.execute(
        text(
            "UPDATE kt_packages SET recipient_user_id = :user, claimed_at = now() "
            "WHERE id = :id AND recipient_user_id IS NULL"
        ),
        {"user": user_id, "id": row.id},
    )
    await _audit_open(
        session,
        org_id=org_id,
        actor_id=user_id,
        action="kt.claimed",
        resource_id=str(row.id),
        outcome="success",
    )
    return row


async def claim_or_open(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, kt_code: str
) -> KtRecipientView:
    """Open (claiming if unclaimed) and return the recipient's view of the package."""
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)

    await session.execute(
        text("UPDATE kt_packages SET last_activity_at = now() WHERE id = :id"),
        {"id": row.id},  # type: ignore[attr-defined]
    )

    scope = list(row.scope)  # type: ignore[attr-defined]
    profile_row = None
    if "profile" in scope:
        profile_row = (
            await session.execute(
                text(
                    "SELECT u.display_name, ep.designation, ep.department FROM users u "
                    "LEFT JOIN employee_profiles ep ON ep.user_id = u.id "
                    "WHERE u.id = :subject"
                ),
                {"subject": row.subject_user_id},  # type: ignore[attr-defined]
            )
        ).first()
    else:
        profile_row = (
            await session.execute(
                text(
                    "SELECT display_name, NULL AS designation, NULL AS department "
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
