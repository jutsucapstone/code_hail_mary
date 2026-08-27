"""Evidence: the source span behind a citation (§15).

```
GET /v1/evidence/{chunk_id}    retrieval:query
```

`retrieval:query` is held by **every** role, and that is §17 being obeyed rather than
relaxed. Roles gate features; ACLs gate data. Asking the corporate memory a question is
the feature every employee is hired to use — which evidence comes back is decided inside
the SQL by `document_acl`, per caller, per request. Holding this permission grants access
to no particular document, and no permission in the catalogue ever will.

The organisation is not a parameter, and neither is the principal set. Both are derived
server-side: the tenant from the session's GUC, the principals from the caller's user id
inside the same transaction. There is nothing on this endpoint a browser can edit to
change what it may see.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from jutsu_core.rbac import Permission
from jutsu_retrieval import fetch_evidence
from pydantic import BaseModel

from jutsu_api.deps import CurrentPrincipal, Db
from jutsu_api.security import GuardedAPIRoute, requires

router = APIRouter(prefix="/v1", tags=["evidence"], route_class=GuardedAPIRoute)


class EvidenceView(BaseModel):
    """One chunk, with everything a citation needs to render (§12 `Citation`).

    `char_start` and `char_end` index the **original** document, while `text` is the
    masked body the model was given. That is deliberate and it is the trap CLAUDE.md
    records: highlighting the returned text with these offsets mis-highlights the span,
    because masking changes lengths. They address the source document, not this string.
    """

    chunk_id: str
    document_id: str
    document_title: str
    source_system: str
    #: Masked text (§9.1). Never the original body — ADR 0005 is explicit that this still
    #: contains names, but it is what the extraction and answer paths are allowed to read.
    text: str
    char_start: int
    char_end: int
    occurred_at: datetime


@router.get("/evidence/{chunk_id}")
@requires(Permission.RETRIEVAL_QUERY)
async def read_evidence(chunk_id: UUID, principal: CurrentPrincipal, session: Db) -> EvidenceView:
    """The source span behind one citation, if this caller may read it.

    A chunk the caller is not granted is **404, not 403**. A 403 would confirm that the
    chunk exists, which turns this endpoint into an oracle: walk ids, read the status
    codes, and recover the shape of a tenant's document population without ever being
    authorized to read one. `NotFound` is the single answer for never-existed,
    another-tenant's, and not-granted-to-you.
    """
    evidence = await fetch_evidence(session, user_id=principal.user_id, chunk_id=chunk_id)
    return EvidenceView(
        chunk_id=str(evidence.chunk_id),
        document_id=str(evidence.document_id),
        document_title=evidence.document_title,
        source_system=evidence.source_system,
        text=evidence.text,
        char_start=evidence.char_start,
        char_end=evidence.char_end,
        occurred_at=evidence.occurred_at,
    )
