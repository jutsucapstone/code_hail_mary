"""Folders: where a source keeps documents, found by name (ADR 0029).

A folder is not a document. It has no text of its own and nobody is ever granted one. JUTSU
records where each document lives (`documents.folder_path`) and the words of that path
(`document_folder_words`), and a folder exists to a caller exactly when a document inside it
does. So a folder search is a document search — under the same authorization, over the path's
words instead of the text — grouped by path:

    a question that asks where something is
      → its words that could name a folder
      → authorized documents whose folder has them, most words matched first
      → grouped by folder, a few titles each
      → one piece of evidence per folder, citing a document kept in it

**No new grant, no copied content.** A folder's evidence carries its documents' titles and
none of their text. A caller reaches a folder only through a document they may already read:
their own (`ACL_PREDICATE`) for Ask JUTSU, a package's (`KT_PACKAGE_PREDICATE`) for Ask KT.
The tenant is the session's, and the authorization runs inside the SQL like every predicate
in `jutsu_retrieval.search`. A folder whose documents a caller cannot read does not exist for
them — not as a name, not as a count.

**Words by equality, because of row-level security.** The application role is subject to RLS,
and PostgreSQL never uses a non-leakproof operator as an index condition beneath a policy.
Full-text `@@` is not leakproof, so an expression GIN index over the path served the owner and
never `jutsu_app`; `text` equality is, so `ix_document_folder_words_word` serves the role
production runs as. Measured at 50,000 documents: 0.4 to 0.7 ms, against 68 to 276 ms.

**Only for a question that asks where.** Words most folders share ("notes", "drive") match most
of a tenant, and authorizing and ranking all of them costs hundreds of milliseconds under any
design. A question that does not ask where something is reads no folder; its passages still
carry their own folders to the model (`jutsu_api.answers`).

**Cited through a real document.** Each folder's evidence cites the newest matching document
in it by that document's first chunk, so a citation opens something that really is there.

Like `search_chunks` and `search_subject_chunks`, neither function takes a principal set, a
group set or an org id; principals are resolved inside, in the caller's transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from jutsu_db.acl import resolve_acl_principals, resolve_subject_principals
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_retrieval.search import (
    ACL_PREDICATE,
    KT_PACKAGE_PREDICATE,
    ORG_SCOPE_SQL,
    RetrievalWindow,
)
from jutsu_retrieval.terms import query_terms, words_of

__all__ = [
    "FOLDER_LIMIT",
    "FolderEvidence",
    "folder_terms",
    "search_folders",
    "search_subject_folders",
]

#: The most folders one question reads.
FOLDER_LIMIT: Final = 5

#: The most document titles one folder's evidence names.
_TITLES_PER_FOLDER: Final = 5

#: How many matching documents are grouped into folders, at most. The bound on what a
#: question returns, so a question matching a thousand documents' paths still reads fifty rows.
_CANDIDATES: Final = 50

#: Words that make a question one about where something is. Without one, no folder is read.
_LOCATION_INTENT: Final = frozenset(
    {"where", "folder", "folders", "directory", "directories", "path", "paths", "stored",
     "kept", "located", "location", "saved"}
)  # fmt: skip

#: Words a question about a location uses that say nothing about which folder it means.
_LOCATION_WORDS: Final = frozenset(
    {"folder", "folders", "directory", "directories", "path", "paths", "stored", "store",
     "stores", "kept", "keep", "keeps", "located", "location", "lives", "live", "saved",
     "save", "documents", "document", "docs", "files", "file", "find", "where"}
)  # fmt: skip

#: The documents whose folder has any of the question's words, and how many of them. Equality
#: on `word`, which is leakproof, so `ix_document_folder_words_word` is an index condition for
#: the application role under row-level security (migration 0025).
_MATCHED: Final = (
    "(SELECT w.document_id, count(*) AS matched FROM document_folder_words w "  # noqa: S608
    f"WHERE w.org_id = {ORG_SCOPE_SQL} AND w.word = ANY(CAST(:words AS text[])) "
    "GROUP BY w.document_id) m"
)


def _statement(predicate: str) -> str:
    """The folder scan with one of this package's two authorization constants.

    `predicate` is always `ACL_PREDICATE` or `KT_PACKAGE_PREDICATE`, chosen below at import
    time — never a string a caller supplies.
    """
    return (
        "SELECT d.id AS document_id, d.title AS document_title, "  # noqa: S608
        "d.created_at AS occurred_at, d.folder_path, d.folder_uri, "
        "CAST(s.system AS text) AS source_system, "
        "c.id AS chunk_id, c.char_start, c.char_end, m.matched AS rank "
        f"FROM {_MATCHED} "
        "JOIN documents d ON d.id = m.document_id "
        "JOIN sources s ON s.id = d.source_id "
        "JOIN LATERAL (SELECT ch.id, ch.char_start, ch.char_end FROM chunks ch "
        "WHERE ch.document_id = d.id ORDER BY ch.ordinal LIMIT 1) c ON true "
        f"WHERE d.org_id = {ORG_SCOPE_SQL} AND d.superseded_by IS NULL "
        "AND d.folder_path IS NOT NULL "
        f"AND {predicate} "
        "ORDER BY rank DESC, d.created_at DESC, d.id DESC "
        "LIMIT :candidates"
    )


#: A caller's own folders: `ACL_PREDICATE` over their resolved principals.
FOLDERS_STATEMENT: Final = _statement(ACL_PREDICATE)

#: A package's folders: `KT_PACKAGE_PREDICATE` over its subject, attachments and exclusions.
SUBJECT_FOLDERS_STATEMENT: Final = _statement(KT_PACKAGE_PREDICATE)


@dataclass(frozen=True, slots=True)
class FolderEvidence:
    """One folder, as a numbered item an answer can cite.

    The citation is a real document kept in the folder: its id, title, source and the span
    of its first chunk. `text` is the folder's path and the titles found in it.
    """

    chunk_id: UUID
    document_id: UUID
    document_title: str
    source_system: str
    text: str
    char_start: int
    char_end: int
    score: float
    occurred_at: datetime
    folder_path: str
    folder_uri: str | None


def folder_terms(question: str) -> list[str]:
    """The words of a question that could name a folder: none unless it asks where."""
    if _LOCATION_INTENT.isdisjoint(words_of(question)):
        return []
    return query_terms(question, ignore=_LOCATION_WORDS)


def _group(rows: list[Any], limit: int) -> list[FolderEvidence]:
    """Rows arrive best match first, so a folder's first row decides its place."""
    by_folder: dict[str, list[Any]] = {}
    for row in rows:
        by_folder.setdefault(str(row.folder_path), []).append(row)

    evidence: list[FolderEvidence] = []
    for path, members in list(by_folder.items())[:limit]:
        newest = max(members, key=lambda row: row.occurred_at)
        titles: list[str] = []
        for member in members:
            if member.document_title not in titles:
                titles.append(member.document_title)
        shown = titles[:_TITLES_PER_FOLDER]
        extra = len(titles) - len(shown)
        listing = "; ".join(shown) + (f"; and {extra} more" if extra > 0 else "")
        evidence.append(
            FolderEvidence(
                chunk_id=UUID(str(newest.chunk_id)),
                document_id=UUID(str(newest.document_id)),
                document_title=str(newest.document_title),
                source_system=str(newest.source_system),
                text=f"Folder: {path}\nDocuments kept in it: {listing}",
                char_start=int(newest.char_start),
                char_end=int(newest.char_end),
                score=float(members[0].rank),
                occurred_at=newest.occurred_at,
                folder_path=path,
                folder_uri=newest.folder_uri,
            )
        )
    return evidence


async def search_folders(
    session: AsyncSession, *, user_id: UUID, question: str, limit: int = FOLDER_LIMIT
) -> list[FolderEvidence]:
    """The folders a question asking where names, among documents this caller may read."""
    terms = folder_terms(question)
    if not terms:
        return []
    principals, groups = await resolve_acl_principals(session, user_id=user_id)
    rows = (
        await session.execute(
            text(FOLDERS_STATEMENT),
            {
                "words": terms,
                "principals": sorted(principals),
                "groups": sorted(groups),
                "candidates": _CANDIDATES,
            },
        )
    ).all()
    return _group(list(rows), max(1, min(limit, FOLDER_LIMIT)))


async def search_subject_folders(
    session: AsyncSession,
    *,
    subject_user_id: UUID,
    package_id: UUID,
    within: RetrievalWindow | None,
    question: str,
    limit: int = FOLDER_LIMIT,
) -> list[FolderEvidence]:
    """The folders a question asking where names, among one knowledge-transfer package's
    documents.

    Only ever called with a scope from an opened package — `jutsu_api.kt_search`, which a
    structural test pins as the one caller, exactly as for `search_subject_chunks`.
    """
    terms = folder_terms(question)
    if not terms:
        return []
    principals = await resolve_subject_principals(session, subject_user_id=subject_user_id)
    rows = (
        await session.execute(
            text(SUBJECT_FOLDERS_STATEMENT),
            {
                "words": terms,
                "subject_principals": sorted(principals),
                "package_id": str(package_id),
                "window_start": within.created_from if within is not None else None,
                "window_end": within.created_to if within is not None else None,
                "candidates": _CANDIDATES,
            },
        )
    ).all()
    return _group(list(rows), max(1, min(limit, FOLDER_LIMIT)))
