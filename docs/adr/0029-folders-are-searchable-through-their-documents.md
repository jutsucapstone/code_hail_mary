# ADR 0029 — Folders are searchable through the documents kept in them

**Status:** accepted
**Date:** 2026-09-15
**Related:** ADR 0011 (retrieval ACL), ADR 0014 (`owner_acl`), ADR 0025 (a package reads its
subject), ADR 0027 (what a package carries), ADR 0028 (Ask KT reads claims)

## Context

"Where are A's Astro Agent documents stored?" had no answer anywhere in JUTSU. A document row held
a title, a body and a `uri`. Nothing recorded the folder a source keeps it in, and a passage rarely
says where its own file lives. Ask JUTSU and Ask KT could answer a location question only when a
document happened to mention a path in its text.

Three shapes were available, and two of them break the invariant:

- **A folder as a document** — a row holding its children's text. It duplicates content outside
  every per-document check, and it needs an ACL no connector can supply: a folder's sharing is not
  the union of its children's, and providers report sharing as email addresses, which are not
  subjects (ADR 0014).
- **A folder table with grants of its own.** The same guessed ACL, plus a second authorization
  surface to keep in step with the first.
- **A folder as a property of a document**, read under that document's own authorization.

## Decision

### 1. Where a document is kept is a column on the document

Migration 0025 adds two nullable columns to `documents`:

- `folder_path` — a path a person reads;
- `folder_uri` — a link to the folder, when the provider gives one.

NULL means unknown. Nothing is derived from a title or guessed from a URL's shape.

**The path's words are rows, matched by equality.** `document_folder_words (org_id, document_id,
word)` holds each document's path words, read by `jutsu_retrieval.terms.folder_words` exactly as a
question's words are read: letters and digits, three characters or more. The table has RLS `ENABLE`
+ `FORCE` and a btree on `(org_id, word, document_id)`. Words are replaced, never edited — the
application role may insert and delete them, and `UPDATE` is revoked.

The first design was an expression GIN index over the path's `tsvector`, and it could never serve
production. The application role is subject to row-level security, and PostgreSQL does not use a
non-leakproof operator as an index condition beneath a policy. In `pg_proc`, `ts_match_vq` (`@@`),
`arrayoverlap` (`&&`), `arraycontains` (`@>`) and `textlike` (`LIKE`) are not leakproof; `texteq`
and `uuid_eq` are. Measured in `jutsu_test` with 50,000 documents, two of them in a matching folder
and every one readable by the asker:

| Grants | Surface | GIN, app role | GIN, owner (bypasses RLS) | Word table, app role |
|---|---|---|---|---|
| user | Ask JUTSU | 67.9 ms | 72.7 ms | 0.44 ms |
| user | Ask KT | 276.0 ms | 9.1 ms, index used | 0.60 ms |
| org-wide | Ask JUTSU | 269.1 ms | 9.6 ms, index used | 0.66 ms |

The owner's plans are the ones a test run as the migration role would have reported. The application
role never receives them.

### 2. Connectors record what the provider says

| Source | `folder_path` | `folder_uri` | Extra requests |
|---|---|---|---|
| Local corpus | the corpus-relative directory | none | none |
| OneDrive | `OneDrive/` + `parentReference.path` after `root:`, percent-decoded | the file's `webUrl` without its last segment | none: the item GET the fetch already makes |
| SharePoint | `SharePoint/` + the same path | the same | none |
| Google Drive | the `parents` chain, e.g. `My Drive/Projects/Astro Agent` | `drive.google.com/drive/folders/{parent}` | one `files.get` per level, cached per connector, at most 8 levels |
| GitHub | `GitHub/{owner}/{repo}` | the repository | none |
| Gmail, Calendar, Meet, Teams, Slack, Jira, Confluence, Zoom, Knowledge Basket | NULL | NULL | none |

**A Drive parent the connecting account cannot read answers 404.** The walk ends at what it
resolved, and the document is still ingested. A 401 or a transient failure fails the fetch exactly
as it would for the file itself.

Confluence spaces, Jira projects and Slack channels are container-like but are not recorded as
folders. Naming each one is a decision for its own surface.

### 3. A folder is metadata, not content

The folder is not part of `content_hash`. When a re-fetched body is unchanged but its folder differs,
the pipeline updates `folder_path`, `folder_uri` and the document's folder words on the current
version in place. A move creates
no new version, and nothing is re-chunked or re-embedded.

### 4. A folder search is a document search

`jutsu_retrieval.folders` matches the question's words against `document_folder_words` by
`word = ANY(...)`, ranks documents by how many of those words their folder has, then groups them
by path.

- **`search_folders(session, user_id, question)`** — `ACL_PREDICATE` over the caller's own
  principals, for Ask JUTSU.
- **`search_subject_folders(session, subject_user_id, package_id, within, question)`** —
  `KT_PACKAGE_PREDICATE`, for Ask KT: the subject's documents inside the period, the attached
  basket files, minus exclusions. It has exactly one caller, `kt_search.folders_for_question`, and
  the structural test beside `search_subject_chunks` pins that.

Neither function takes a principal set or an organisation. The tenant is the session's GUC, and
the predicate runs inside the SQL.

**Only a question that asks where reads folders.** It needs a location word — `where`, `folder`,
`directory`, `path`, `stored`, `kept`, `located`, `location` or `saved` — or the folder search
returns nothing without touching the database. The words come from `jutsu_retrieval.terms`:
letters and digits only, bound as one `text[]` parameter, with location framing (`where`,
`stored`, `folder`, `documents` and similar) dropped.

The gate exists because words most folders share match most of a tenant, and authorizing and
ranking all of those documents is expensive under either design. Measured with `weekly` and
`notes`, which all 50,000 folders contain:

| Grants | Surface | GIN, app role | Word table, app role |
|---|---|---|---|
| user | Ask JUTSU | 154 ms | 253 ms |
| user | Ask KT | 641 ms | 678 ms |
| org-wide | Ask JUTSU | 572 ms | 177 ms |

A question that does not ask where something is still learns each passage's folder from the
prompt header (section 5).

The bounds:

- at most 50 candidate documents per statement;
- at most 5 folders per question;
- at most 5 titles per folder.

### 5. A folder's evidence is titles, cited through a real document

Each folder becomes one numbered item:

```
Folder: Projects/Astro Agent
Documents kept in it: plan.md; risks.md; and 3 more
```

**No passage text is copied into it.** It is cited through the first chunk of the newest matching
document. The citation therefore opens a document the asker may already read, through the door that
document already has: `/v1/evidence` for Ask JUTSU, `/v1/kt/{code}/evidence` for Ask KT.

Passages carry their folder too. The prompt header reads `[n] title (source) — folder: path`, so a
retrieved passage can answer a location question as well.

### 6. Surfaces

- `/v1/ask` appends the folder arm after passages. Ask KT appends it after passages and claims,
  and reads folders only when the package covers `documents`, like every raw-document read.
- Sources and citations carry `kind` (`passage`, `claim` or `folder`) and `folder_path`.
- These show where a document is kept:
  - the KT documents tab and the document reader;
  - the curator's review;
  - both evidence panels;
  - the handover report, which also prints a "Where documents are kept" section: the folders
    the package's documents are kept in, with counts, through the same `KtScope`.

## Consequences

- **A folder's name is shown under exactly its document's authorization.** A name like
  `HR/Terminations` can be sensitive. It is visible to precisely those who can open a document in
  it — the exposure a document title already has.
- **Documents synced before migration 0025 have no folder until a walk lists them again.** The
  source cursor keeps unchanged files out of later listings. An existing document gains its folder
  when it changes, or when its source is walked from no cursor. A migration cannot backfill it:
  only the provider knows where the file is.
- **A folder question naming only common words stays slow.** Hundreds of milliseconds at 50,000
  documents (section 4), bounded by the 50-row limit and paid only by questions that ask where.
- **A text search is measured as the application role.** The owner bypasses row-level security and
  gets plans `jutsu_app` never will; measured as the owner, the GIN index here looked thirty times
  faster than it was.
- **Google Drive pays up to eight extra reads per new folder chain**, once per folder per
  connector lifetime. Microsoft, GitHub and the local corpus pay nothing.
- **Not built:**
  - folder-level sharing;
  - a folder browser;
  - container names for sources without folders;
  - fuzzy or stemmed folder matching.
