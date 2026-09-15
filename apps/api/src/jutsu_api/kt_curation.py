"""What a knowledge-transfer package shares, reviewed and narrowed by its curator (ADR 0027).

A package carries its subject's own documents inside its period plus the Knowledge Basket
files attached to it — `jutsu_retrieval.search.KT_PACKAGE_RULE`. This module is where
somebody can see that list before a recipient does, and keep a document back.

**Who.** `kt:manage`, or the package's own subject: `jutsu_api.kt.curation_scope`, the rule
attaching a file already follows (ADR 0021). Anybody else — the recipient included — gets
the same 404 as a package that does not exist.

**What a curator sees.** Titles, source systems and dates; never a passage. Excluding a
document takes recognising it, which needs its title and nothing more; reading it is the
recipient's capability and is not granted here. The first page of every review writes an
audit row, because it is a look at another employee's document list.

**What an exclusion is.** A row keyed by the document's stable identity, read inside every
KT statement through `KT_PACKAGE_PREDICATE`. It takes effect on the recipient's next request
with nothing to invalidate, and it survives a re-sync that versions the document.
Withdrawing a document stays allowed on a closed package; putting one back does not, for
the reason detaching and attaching a file differ (ADR 0021).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from jutsu_core.errors import Conflict, NotFound
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.kt import KtScope, curation_scope

if TYPE_CHECKING:
    from jutsu_api.security import Principal

__all__ = [
    "KtContentItem",
    "KtContentPage",
    "exclude_document",
    "include_document",
    "package_contents",
]

_CLOSED = "That package is closed, so nothing it left out can be put back."
_NOT_IN_PACKAGE = "That document is not part of this package."
_NOT_EXCLUDED = "That document is not excluded from this package."


@dataclass(frozen=True, slots=True)
class KtContentItem:
    document_id: UUID
    title: str
    source_system: str
    created_at: datetime
    #: A Knowledge Basket file attached to this package, rather than a document from one of
    #: the subject's connected applications.
    attached_file: bool
    #: Kept back from the recipient.
    excluded: bool
    #: Where the source keeps it, when it says (ADR 0029).
    folder_path: str | None = None


@dataclass(frozen=True, slots=True)
class KtContentPage:
    items: list[KtContentItem]
    next_cursor: str | None


#: What a review shows, and whether each row is excluded — computed in the same statement,
#: so a page and its flags cannot disagree. `:package_id` is bound by the scope's conditions.
_ITEM_COLUMNS = (
    "d.id, d.title, d.created_at, d.source_id, d.external_id, d.folder_path, "
    "CAST(s.system AS text) AS source_system, "
    "EXISTS (SELECT 1 FROM kt_package_exclusions kx "
    "WHERE kx.package_id = CAST(:package_id AS uuid) "
    "AND kx.source_id = d.source_id AND kx.external_id = d.external_id) AS excluded"
)


def _item(row: Any) -> KtContentItem:
    return KtContentItem(
        document_id=UUID(str(row.id)),
        title=str(row.title),
        source_system=str(row.source_system),
        created_at=row.created_at,
        attached_file=row.source_system == "basket",
        excluded=bool(row.excluded),
        folder_path=row.folder_path,
    )


async def _audit(
    session: AsyncSession,
    *,
    actor: Principal,
    action: str,
    package_id: UUID,
    correlation_id: str | None,
    meta: dict[str, object],
) -> None:
    """One trail row: the package and, at most, an opaque document id — never a title (§4.9)."""
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome, correlation_id, meta_json) "
            "VALUES (:org, :actor, 'user', :action, 'kt_package', :rid, 'success', "
            ":correlation, cast(:meta AS jsonb))"
        ),
        {
            "org": str(actor.org_id),
            "actor": str(actor.user_id),
            "action": action,
            "rid": str(package_id),
            "correlation": correlation_id,
            "meta": json.dumps(meta),
        },
    )


async def _in_package(session: AsyncSession, *, scope: KtScope, document_id: UUID) -> Any:
    """One document the package's rule covers, or the 404 an exclusion cannot get past."""
    params: dict[str, object] = {"id": document_id}
    filters = ["d.id = :id", *scope.curation_conditions(params)]
    row = (
        await session.execute(
            text(
                f"SELECT {_ITEM_COLUMNS} FROM documents d "  # noqa: S608
                "JOIN sources s ON s.id = d.source_id "
                f"WHERE {' AND '.join(filters)}"
            ),
            params,
        )
    ).first()
    if row is None:
        raise NotFound(_NOT_IN_PACKAGE)
    return row


async def package_contents(
    session: AsyncSession,
    *,
    actor: Principal,
    package_id: UUID,
    limit: int,
    cursor: str | None,
    correlation_id: str | None = None,
) -> KtContentPage:
    """Every document the package's rule covers, newest first, each flagged if excluded."""
    _row, scope = await curation_scope(session, actor=actor, package_id=package_id)

    bounded = max(1, min(limit, 100))
    params: dict[str, object] = {"limit": bounded + 1}
    filters = scope.curation_conditions(params)
    if cursor:
        try:
            ts, last_id = cursor.split("|", 1)
            params["cursor_ts"] = datetime.fromisoformat(ts)
            params["cursor_id"] = UUID(last_id)
        except (ValueError, AttributeError) as exc:
            raise NotFound("That page does not exist.") from exc
        filters.append("(d.created_at, d.id) < (:cursor_ts, :cursor_id)")

    rows = (
        await session.execute(
            text(
                f"SELECT {_ITEM_COLUMNS} FROM documents d "  # noqa: S608
                "JOIN sources s ON s.id = d.source_id "
                f"WHERE {' AND '.join(filters)} "
                "ORDER BY d.created_at DESC, d.id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()
    page = rows[:bounded]
    next_cursor = (
        f"{page[-1].created_at.isoformat()}|{page[-1].id}" if len(rows) > bounded and page else None
    )

    if cursor is None:
        await _audit(
            session,
            actor=actor,
            action="kt.contents_reviewed",
            package_id=scope.package_id,
            correlation_id=correlation_id,
            meta={"listed": len(page)},
        )
    return KtContentPage(items=[_item(row) for row in page], next_cursor=next_cursor)


async def exclude_document(
    session: AsyncSession,
    *,
    actor: Principal,
    package_id: UUID,
    document_id: UUID,
    correlation_id: str | None = None,
) -> KtContentItem:
    """Keep one document back from the package's recipient, from their next request on.

    Idempotent: excluding an excluded document writes no second row and no second audit
    row. A document outside the package's rule is a 404 — an exclusion can only narrow.
    """
    _row, scope = await curation_scope(session, actor=actor, package_id=package_id)
    target = await _in_package(session, scope=scope, document_id=document_id)

    inserted = (
        await session.execute(
            text(
                "INSERT INTO kt_package_exclusions (id, org_id, package_id, source_id, "
                "external_id, document_id, excluded_by) "
                "VALUES (:id, :org, :pkg, :source, :external, :doc, :by) "
                "ON CONFLICT (package_id, source_id, external_id) DO NOTHING RETURNING id"
            ),
            {
                "id": uuid4(),
                "org": str(actor.org_id),
                "pkg": scope.package_id,
                "source": target.source_id,
                "external": target.external_id,
                "doc": document_id,
                "by": str(actor.user_id),
            },
        )
    ).first()
    if inserted is not None:
        await _audit(
            session,
            actor=actor,
            action="kt.document_excluded",
            package_id=scope.package_id,
            correlation_id=correlation_id,
            meta={"document_id": str(document_id)},
        )
    return replace(_item(target), excluded=True)


async def include_document(
    session: AsyncSession,
    *,
    actor: Principal,
    package_id: UUID,
    document_id: UUID,
    correlation_id: str | None = None,
) -> None:
    """Put an excluded document back. Refused on a closed package, because it widens."""
    row, scope = await curation_scope(session, actor=actor, package_id=package_id)
    if row.revoked_at is not None or row.completed_at is not None:  # type: ignore[attr-defined]
        raise Conflict(_CLOSED)
    target = await _in_package(session, scope=scope, document_id=document_id)

    deleted = (
        await session.execute(
            text(
                "DELETE FROM kt_package_exclusions WHERE package_id = :pkg "
                "AND source_id = :source AND external_id = :external RETURNING id"
            ),
            {"pkg": scope.package_id, "source": target.source_id, "external": target.external_id},
        )
    ).first()
    if deleted is None:
        raise NotFound(_NOT_EXCLUDED)
    await _audit(
        session,
        actor=actor,
        action="kt.document_included",
        package_id=scope.package_id,
        correlation_id=correlation_id,
        meta={"document_id": str(document_id)},
    )
