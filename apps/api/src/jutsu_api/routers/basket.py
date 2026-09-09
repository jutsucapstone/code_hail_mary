"""The Knowledge Basket over HTTP.

  POST   /v1/basket/files              basket:write — reserve a row, return an upload URL
  POST   /v1/basket/files/{id}/complete basket:write — verify the bytes, start ingestion
  GET    /v1/basket/files              basket:write — your basket, or the org's with manage
  GET    /v1/basket/files/{id}/download basket:write — a short-lived signed GET
  PATCH  /v1/basket/files/{id}          basket:write — rename
  POST   /v1/basket/files/{id}/retry    basket:write — re-run a failed extraction
  DELETE /v1/basket/files/{id}          basket:write — soft delete

Every route takes `basket:write`, which every role holds, and the *ownership* boundary is
applied inside the query rather than by the gate — you see your own files, and a holder of
`basket:manage` sees the organisation's. Gating the routes themselves on the admin
permission would make the feature unusable by the people it exists for; gating them on
nothing would make one employee's files reachable by another. The permission says "may
use a basket"; the query says "whose".

Bytes never pass through this process. The browser PUTs to Cloud Storage under a URL
signed for one object, one type and one size, and `complete` is the gate that decides
whether what landed is trustworthy (ADR 0020).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from jutsu_core.rbac import Permission
from jutsu_core.storage import MAX_UPLOAD_BYTES
from pydantic import BaseModel, Field

from jutsu_api import basket
from jutsu_api.deps import CurrentPrincipal, Db, StoreDep
from jutsu_api.queue import ring_doorbell
from jutsu_api.security import GuardedAPIRoute, requires

router = APIRouter(prefix="/v1/basket", tags=["basket"], route_class=GuardedAPIRoute)


class UploadRequest(BaseModel):
    model_config = {"extra": "forbid"}

    #: As the person's filesystem had it. Stored for display and download; it never
    #: reaches an object key, so no sanitisation here is load-bearing.
    filename: str = Field(min_length=1, max_length=512)
    #: A hint, used to pin the signed URL. `complete` reads the bytes and decides.
    content_type: str = Field(min_length=3, max_length=255)
    size_bytes: int = Field(gt=0, le=MAX_UPLOAD_BYTES)


class UploadTicketOut(BaseModel):
    """Everything the browser needs to PUT the file, and nothing else."""

    file_id: UUID
    url: str
    #: Must be sent verbatim: they were signed, so changing one makes the PUT fail at
    #: Cloud Storage rather than here.
    headers: dict[str, str]
    expires_in_seconds: int


class BasketFileOut(BaseModel):
    id: UUID
    owner_user_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    #: uploading · uploaded · validating · extracting · chunking · embedding · ready ·
    #: stored · rejected · failed · quarantined
    state: str
    #: One sentence for the reader — why it was refused, or why it is stored but not
    #: searchable. Never a stack trace and never an internal path.
    detail: str | None
    #: Whether this file's text is in the corpus. `stored` files are kept and
    #: downloadable but deliberately not indexed.
    searchable: bool
    #: Characters extracted. Zero is a real answer — a scan with no text layer — and
    #: distinguishing it from null is what lets the interface say which.
    extracted_chars: int | None
    #: Whether a retry would do anything, decided server-side so the button cannot
    #: appear where it would fail.
    retryable: bool
    created_at: datetime
    updated_at: datetime


class BasketPage(BaseModel):
    items: list[BasketFileOut]


class DownloadOut(BaseModel):
    url: str


def _out(row: basket.BasketFileRow) -> BasketFileOut:
    return BasketFileOut(
        id=row.id,
        owner_user_id=row.owner_user_id,
        filename=row.original_filename,
        content_type=row.detected_mime or row.declared_mime,
        size_bytes=row.size_bytes,
        state=row.state,
        detail=row.failure_reason,
        searchable=row.state == "ready",
        extracted_chars=row.extracted_chars,
        retryable=row.state == "failed",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("/files", status_code=status.HTTP_201_CREATED)
@requires(Permission.BASKET_WRITE)
async def create_upload(
    payload: UploadRequest, principal: CurrentPrincipal, session: Db, store: StoreDep
) -> UploadTicketOut:
    """Reserve a row and mint a capability for exactly one object.

    The row is written before the bytes exist, so an upload the browser abandons is a
    visible `uploading` row rather than nothing at all — which is what lets the interface
    show it and the lifecycle rule clean up after it.
    """
    ticket = await basket.start_upload(
        session,
        actor=principal,
        store=store,
        filename=payload.filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
    )
    return UploadTicketOut(
        file_id=ticket.file_id,
        url=ticket.upload.url,
        headers=ticket.upload.headers,
        expires_in_seconds=ticket.upload.expires_in_seconds,
    )


@router.post("/files/{file_id}/complete")
@requires(Permission.BASKET_WRITE)
async def complete_upload(
    file_id: UUID, principal: CurrentPrincipal, session: Db, store: StoreDep
) -> BasketFileOut:
    """Verify what landed, and start extraction if there is text to reach.

    Returns 200 with the row's real state rather than an error when the file is refused:
    a rejected upload is a normal outcome the interface renders per file, and turning it
    into a 4xx would make one bad file in a multi-file drop look like a failed request.
    """
    row = await basket.complete_upload(session, actor=principal, store=store, file_id=file_id)
    # `complete_upload` enqueues `ingest.document`; a durable row is not a running job.
    # Every other enqueue in the API rings (a connector sync, the Jobs page, a sign-in),
    # and this one did not — so in production an uploaded file sat `pending` until some
    # unrelated doorbell happened to drain the organisation, and the console polled a
    # row nothing was going to change (ADR 0017). Best-effort by construction: the row
    # is committed either way and the next ring drains it.
    await ring_doorbell(principal.org_id)
    return _out(row)


@router.get("/files")
@requires(Permission.BASKET_WRITE)
async def list_files(
    principal: CurrentPrincipal,
    session: Db,
    q: Annotated[str | None, Query(max_length=200)] = None,
    state: Annotated[str | None, Query(max_length=24)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> BasketPage:
    """Your files. An administrator holding `basket:manage` sees the organisation's."""
    rows = await basket.list_files(session, actor=principal, query=q, state=state, limit=limit)
    return BasketPage(items=[_out(row) for row in rows])


@router.get("/files/{file_id}/download")
@requires(Permission.BASKET_WRITE)
async def download(
    file_id: UUID, principal: CurrentPrincipal, session: Db, store: StoreDep
) -> DownloadOut:
    """A signed GET, minted only after the row came back under the caller's scope.

    Returned as JSON rather than a redirect so the browser fetches it deliberately — a
    302 to a signed URL ends up in history, in referrer headers and in server logs.
    """
    return DownloadOut(
        url=await basket.download_url(session, actor=principal, store=store, file_id=file_id)
    )


class RenamePayload(BaseModel):
    model_config = {"extra": "forbid"}

    filename: str = Field(min_length=1, max_length=512)


@router.patch("/files/{file_id}")
@requires(Permission.BASKET_WRITE)
async def rename(
    file_id: UUID, payload: RenamePayload, principal: CurrentPrincipal, session: Db
) -> BasketFileOut:
    row = await basket.rename_file(
        session, actor=principal, file_id=file_id, filename=payload.filename
    )
    return _out(row)


@router.post("/files/{file_id}/retry", status_code=status.HTTP_202_ACCEPTED)
@requires(Permission.BASKET_WRITE)
async def retry(file_id: UUID, principal: CurrentPrincipal, session: Db) -> BasketFileOut:
    """Re-run a failed extraction. Only `failed` qualifies — see `retry_file`."""
    row = await basket.retry_file(session, actor=principal, file_id=file_id)
    # A retry re-opens the job; without a ring it waits exactly as the first attempt did.
    await ring_doorbell(principal.org_id)
    return _out(row)


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
@requires(Permission.BASKET_WRITE)
async def delete(
    file_id: UUID, principal: CurrentPrincipal, session: Db, store: StoreDep
) -> Response:
    """Remove the listing, the search grant and the bytes.

    `store` may be None — a deployment without storage still lets a row be removed, and
    the object it would have deleted does not exist.
    """
    await basket.delete_file(session, actor=principal, store=store, file_id=file_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
