"""Basket files attached to a knowledge-transfer package (ADR 0021).

Two audiences, two authorization paths, and keeping them apart is the whole module.

**The recipient** reaches a file through `_open_for` and nothing else. That function is
the KT session — it re-decides binding, revocation, completion and expiry on every
request from the cookie principal plus the code, and it spends a `KT_OPEN` allowance
before it looks anything up. So `shared_files` and `shared_download_url` open the package
first and join second; there is no path here that reads `kt_package_files` without having
opened the package for that exact caller.

**The administrator** reaches the same table through `kt:manage`, and additionally must
already be able to see the file under `basket.visible_to`. Attaching is not a way to
reach a file you could not otherwise reach, and `kt:manage` is deliberately not widened
into a read over anybody's basket — an HR Admin can create a package and cannot attach
the subject's files, because seeing another employee's uploads is `basket:manage`.

**A row in `kt_package_files` is a reference, never a permission.** Nothing in it says
"may read"; the grant is computed per request from the package's live state. Revoking a
package therefore closes access to its files with no code running at revocation time, and
there is no grant row anywhere that could outlive the thing that justified it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

from jutsu_core.errors import Conflict, NotFound, ServiceUnavailable
from jutsu_core.rbac import Permission
from jutsu_core.storage import ObjectStore
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.basket import visible_to
from jutsu_api.kt import open_package_for
from jutsu_api.security import Principal

logger = logging.getLogger("jutsu.kt.files")

__all__ = [
    "SharedFileRow",
    "attach_files",
    "attachable_files",
    "detach_file",
    "package_attachments",
    "shared_download_url",
    "shared_files",
]

#: The states a file may NOT be shared in. Written as the exclusion rather than the
#: inclusion, and that is load-bearing.
#:
#: The obvious spelling — share only `ready` and `stored` — is wrong twice. A file is
#: `uploaded` for as long as the queue takes, so an administrator building a handover
#: minutes after the employee uploaded would find nothing attachable; and `ready` is
#: reached only after the EMBEDDING job completes, so in a deployment with no embedding
#: provider configured (a documented, supported state) no file ever becomes shareable at
#: all. Sharing would be permanently impossible and nothing would say why.
#:
#: What actually matters is whether the bytes are trustworthy and present:
#:
#:   * `uploading` — no bytes are guaranteed to exist; the row precedes the object;
#:   * `rejected` — the bytes were not what they claimed;
#:   * `quarantined` — refused for a security reason.
#:
#: Everything else has bytes that `complete_upload` verified. That includes `failed`,
#: where extraction broke but the document is intact and downloadable — the bytes are the
#: knowledge, and extraction only decides whether it is also searchable.
_UNSHAREABLE: Final = ("uploading", "rejected", "quarantined")

#: One sentence for "no such package" and for "not yours to curate", so the two cannot be
#: told apart from the outside.
_NO_PACKAGE: Final = "That package was not found."

#: The same tuple as a SQL literal. Spelled once rather than rendered per query, so the
#: three places that filter on it cannot drift — and built from `_UNSHAREABLE` so the
#: Python membership test and the SQL predicate are the same set by construction.
_UNSHAREABLE_SQL: Final = "(" + ", ".join(f"'{state}'" for state in _UNSHAREABLE) + ")"

#: Columns the recipient sees. Deliberately narrower than `BasketFileRow`: no owner id, no
#: object key, no failure reason, no attempt count. A recipient is told what the file is
#: and whether its text is in the corpus, and nothing about the machinery.
_RECIPIENT_COLUMNS: Final = (
    "f.id, f.original_filename, f.declared_mime, f.detected_mime, f.size_bytes, "
    "f.state, f.extracted_chars, f.created_at, a.attached_at"
)

# S608 appears on the queries below and the exemption is the one `basket.py` states: the
# only interpolated fragments are module-level literals and the two-literal `scope`
# returned by `visible_to`. Every caller-supplied value is a bound parameter.


@dataclass(frozen=True, slots=True)
class SharedFileRow:
    """One attached file, as the recipient sees it."""

    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    #: The owner's own state word — `ready` (its text is in the corpus), `stored` (kept and
    #: downloadable, not searchable), or a still-processing state. Passed through rather than
    #: flattened to a boolean, so the two consoles cannot describe one file differently.
    state: str
    #: Characters extracted. Zero is a real answer — a scan with no text layer.
    extracted_chars: int | None
    uploaded_at: datetime
    attached_at: datetime


@dataclass(frozen=True, slots=True)
class AttachmentRow:
    """One attachment, as an administrator sees it. Carries the owner, which the
    recipient's view deliberately does not."""

    id: UUID
    file_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    state: str
    attached_at: datetime
    attached_by: UUID


def _shared(record: Any) -> SharedFileRow:
    return SharedFileRow(
        id=UUID(str(record.id)),
        filename=record.original_filename,
        # What the file actually is, preferring the sniffed answer over the claimed one —
        # the same precedence `complete_upload` used when it decided the row's fate.
        content_type=record.detected_mime or record.declared_mime,
        size_bytes=int(record.size_bytes),
        state=record.state,
        extracted_chars=record.extracted_chars,
        uploaded_at=record.created_at,
        attached_at=record.attached_at,
    )


# --------------------------------------------------------------------- recipient side


async def shared_files(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, kt_code: str
) -> list[SharedFileRow]:
    """Every live attachment on a package the caller may currently open.

    One `_open_for` and one join, not one open per file: the allowance is spent for the
    act of reading the package, and charging a recipient twenty times for opening one
    panel would exhaust `KT_OPEN` in a single page load.
    """
    package_id = await open_package_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)

    rows = (
        await session.execute(
            text(
                f"SELECT {_RECIPIENT_COLUMNS} "  # noqa: S608
                "FROM kt_package_files a "
                "JOIN basket_files f ON f.id = a.basket_file_id AND f.org_id = a.org_id "
                "WHERE a.package_id = :pkg "
                "  AND a.detached_at IS NULL "
                # The owner's control over their own file outranks the attachment: a file
                # they deleted stops being readable here in the same instant.
                "  AND f.deleted_at IS NULL "
                f"  AND f.state NOT IN {_UNSHAREABLE_SQL} "
                "ORDER BY a.attached_at, f.id"
            ),
            {"pkg": package_id},
        )
    ).all()
    return [_shared(row) for row in rows]


async def shared_download_url(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    store: ObjectStore | None,
    file_id: UUID,
) -> str:
    """A short-lived GET for one attached file, minted only after the grant was verified.

    The order is the security property, exactly as in `basket.download_url`: open the
    package for this caller, find the attachment, read the key off the row, and only then
    sign. A key derived from the request rather than from the row is the mistake this
    shape prevents — and here there is a second one it prevents, which is signing before
    checking that the package is still open.
    """
    if store is None:
        raise ServiceUnavailable("File storage is not configured for this deployment.")

    package_id = await open_package_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)

    record = (
        await session.execute(
            text(
                "SELECT f.object_key, f.original_filename, f.state "
                "FROM kt_package_files a "
                "JOIN basket_files f ON f.id = a.basket_file_id AND f.org_id = a.org_id "
                "WHERE a.package_id = :pkg AND a.basket_file_id = :file "
                "  AND a.detached_at IS NULL AND f.deleted_at IS NULL"
            ),
            {"pkg": package_id, "file": file_id},
        )
    ).first()
    # One 404 for "not attached", "detached", "deleted" and "never existed". A recipient
    # must not be able to tell a file that was withdrawn from one that was never there —
    # the difference would map the subject's basket one id at a time.
    if record is None:
        raise NotFound("That file is not part of this package.")
    if record.state in _UNSHAREABLE:
        raise Conflict("That file is not ready to download yet.")

    return store.signed_download(record.object_key, filename=record.original_filename)


# ------------------------------------------------------------------ administrator side


async def _curatable(session: AsyncSession, *, actor: Principal, package_id: UUID) -> Any:
    """The package row, but only for somebody entitled to change what it shares.

    One function rather than a lookup plus a separate permission check, for the reason
    `_open_for` is one function: two of them is one call site away from doing the lookup
    and forgetting the check.

    Deliberately NOT `_open_for`. That answers "may this caller open this package as its
    recipient", which is a different question and would refuse every administrator. RLS
    keeps this org-scoped; the clause below keeps it authorized.

    `kt:manage` is the permission that already owns package creation. The subject is
    allowed too, because a departing employee curating their own handover is the same act
    by the person whose files they are — and `visible_to` then bounds them to their own
    basket anyway.

    **An unauthorized caller gets the same 404 as a package that does not exist.** A
    package id is an unguessable UUID, so the leak is small, but §5 asks that nothing
    confirm whether another employee's package exists and a distinct 403 here would.
    """
    row = (
        await session.execute(
            text(
                "SELECT id, subject_user_id, revoked_at, completed_at, expires_at, now() AS now "
                "FROM kt_packages WHERE id = :id"
            ),
            {"id": package_id},
        )
    ).first()
    if row is None:
        raise NotFound(_NO_PACKAGE)
    if not (actor.can(Permission.KT_MANAGE) or actor.user_id == UUID(str(row.subject_user_id))):
        raise NotFound(_NO_PACKAGE)
    return row


async def attach_files(
    session: AsyncSession,
    *,
    actor: Principal,
    package_id: UUID,
    file_ids: list[UUID],
) -> int:
    """Attach basket files to a package. Returns how many were newly attached.

    Three bounds, all in the SQL rather than in Python:

      * the file belongs to the package's subject — a handover is one person's knowledge,
        and attaching a third party's file would launder access to somebody who is not
        leaving;
      * the file is visible to the actor under `visible_to` — their own, or any file if
        they hold `basket:manage`;
      * the file is in a shareable state and not deleted.

    A file that fails any of them is silently not attached rather than raising, and the
    count says how many landed. That is deliberate: the alternative tells the caller which
    specific id failed which specific check, which for a `kt:manage` holder without
    `basket:manage` is a probe of somebody else's basket.
    """
    package = await _curatable(session, actor=actor, package_id=package_id)
    if package.revoked_at is not None or package.completed_at is not None:
        raise Conflict("That package is closed. Attachments cannot be changed.")
    if not file_ids:
        return 0

    scope, params = visible_to(actor)
    attached = 0
    for file_id in file_ids:
        eligible = (
            await session.execute(
                text(
                    "SELECT id FROM basket_files "  # noqa: S608
                    "WHERE id = :file AND deleted_at IS NULL "
                    "  AND owner_user_id = :subject "
                    f"  AND state NOT IN {_UNSHAREABLE_SQL} "
                    f"  AND {scope}"
                ),
                {"file": file_id, "subject": str(package.subject_user_id), **params},
            )
        ).scalar_one_or_none()
        if eligible is None:
            continue

        try:
            # A savepoint per row, for the reason `bulk_invitations` documents: Postgres
            # aborts the whole transaction at its first error, so one duplicate would
            # otherwise discard every attachment after it while the request reported
            # success.
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO kt_package_files "
                        "(id, org_id, package_id, basket_file_id, attached_by) "
                        "VALUES (:id, :org, :pkg, :file, :by)"
                    ),
                    {
                        "id": uuid4(),
                        "org": str(actor.org_id),
                        "pkg": package_id,
                        "file": file_id,
                        "by": str(actor.user_id),
                    },
                )
        except IntegrityError:
            # `uq_kt_package_files_live` — already attached. Attaching twice is a no-op,
            # not an error: a double-submitted form must not fail the whole request.
            continue
        attached += 1

    if attached:
        await session.execute(
            text(
                "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
                "resource_id, outcome, meta_json) "
                "VALUES (:org, :actor, 'user', 'kt.files_attached', 'kt_package', :rid, "
                "'success', cast(:meta AS jsonb))"
            ),
            {
                "org": str(actor.org_id),
                "actor": str(actor.user_id),
                "rid": str(package_id),
                # A count, not the filenames: an audit row is read by more people than a
                # basket is, and §4.9 has no carve-out for a field that is not a log line.
                "meta": f'{{"attached": {attached}}}',
            },
        )
    return attached


async def detach_file(
    session: AsyncSession, *, actor: Principal, package_id: UUID, file_id: UUID
) -> None:
    """Stop sharing one file, without touching the package or the file.

    Two columns rather than a DELETE, so the record that a file *was* shared survives its
    unsharing. Allowed on a closed package: withdrawing something is never the act that
    needs blocking.
    """
    # The call IS the authorization: it refuses before anything below runs.
    await _curatable(session, actor=actor, package_id=package_id)

    detached = (
        await session.execute(
            text(
                "UPDATE kt_package_files SET detached_at = now(), detached_by = :by "
                "WHERE package_id = :pkg AND basket_file_id = :file AND detached_at IS NULL "
                "RETURNING id"
            ),
            {"by": str(actor.user_id), "pkg": package_id, "file": file_id},
        )
    ).scalar_one_or_none()
    if detached is None:
        raise NotFound("That file is not attached to this package.")

    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', 'kt.file_detached', 'kt_package', :rid, 'success')"
        ),
        {"org": str(actor.org_id), "actor": str(actor.user_id), "rid": str(package_id)},
    )


async def package_attachments(
    session: AsyncSession, *, actor: Principal, package_id: UUID
) -> list[AttachmentRow]:
    """What a package currently shares, for the administrator who manages it."""
    await _curatable(session, actor=actor, package_id=package_id)

    rows = (
        await session.execute(
            text(
                "SELECT a.id, a.basket_file_id, a.attached_at, a.attached_by, "
                "       f.original_filename, f.declared_mime, f.detected_mime, "
                "       f.size_bytes, f.state "
                "FROM kt_package_files a "
                "JOIN basket_files f ON f.id = a.basket_file_id AND f.org_id = a.org_id "
                "WHERE a.package_id = :pkg AND a.detached_at IS NULL AND f.deleted_at IS NULL "
                "ORDER BY a.attached_at, f.id"
            ),
            {"pkg": package_id},
        )
    ).all()
    return [
        AttachmentRow(
            id=UUID(str(r.id)),
            file_id=UUID(str(r.basket_file_id)),
            filename=r.original_filename,
            content_type=r.detected_mime or r.declared_mime,
            size_bytes=int(r.size_bytes),
            state=r.state,
            attached_at=r.attached_at,
            attached_by=UUID(str(r.attached_by)),
        )
        for r in rows
    ]


async def attachable_files(
    session: AsyncSession, *, actor: Principal, package_id: UUID
) -> list[AttachmentRow]:
    """The subject's basket files this actor could attach, minus the ones already on.

    The picker's data source. Bounded by exactly the same three conditions `attach_files`
    enforces, so the list cannot offer something the write would then refuse — and an
    actor without `basket:manage` sees an empty list rather than a filtered view of
    somebody else's basket.
    """
    package = await _curatable(session, actor=actor, package_id=package_id)

    scope, params = visible_to(actor)
    rows = (
        await session.execute(
            text(
                "SELECT f.id, f.original_filename, f.declared_mime, f.detected_mime, "  # noqa: S608
                "       f.size_bytes, f.state, f.created_at "
                "FROM basket_files f "
                "WHERE f.owner_user_id = :subject AND f.deleted_at IS NULL "
                f"  AND f.state NOT IN {_UNSHAREABLE_SQL} "
                f"  AND {scope} "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM kt_package_files a "
                "    WHERE a.basket_file_id = f.id AND a.package_id = :pkg "
                "      AND a.detached_at IS NULL) "
                "ORDER BY f.created_at DESC, f.id "
                "LIMIT 200"
            ),
            {"subject": str(package.subject_user_id), "pkg": package_id, **params},
        )
    ).all()
    return [
        AttachmentRow(
            id=UUID(str(r.id)),
            file_id=UUID(str(r.id)),
            filename=r.original_filename,
            content_type=r.detected_mime or r.declared_mime,
            size_bytes=int(r.size_bytes),
            state=r.state,
            attached_at=r.created_at,
            attached_by=UUID(str(package.subject_user_id)),
        )
        for r in rows
    ]
