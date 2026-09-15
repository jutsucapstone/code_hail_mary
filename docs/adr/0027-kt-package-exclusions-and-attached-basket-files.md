# ADR 0027 — A package can leave documents out, and basket files join it only when attached

**Status:** accepted
**Date:** 2026-09-15
**Amends:** ADR 0025 (what a package carries); ADR 0021 (its "only what was attached" rule now
governs searching as well as reading)
**Related:** ADR 0010, ADR 0011, ADR 0016, ADR 0020, ADR 0021, ADR 0025

## Context

An isolation audit on 2026-09-15 traced employee C searching employee A's package end to end.
The subject/recipient boundary held. C's session stayed C's, the SQL bound A's principals only,
and nothing crossed from C, a bystander or another tenant.

It found three gaps in what a package *is*, rather than in who reads it:

1. **A package was a rule, not a list.** It carried every document granted directly to A inside
   its period. A's private material from that period reached C with everything else — in the
   audit's fixture, a leave request — and nobody could keep it back.
2. **Whole history was the default.** The wizard's period started at "Full history", and an API
   call without `period_days` silently meant the same.
3. **The create route said it created no access.** The `POST /v1/kt` docstring and the web
   client's comment still described the design before ADR 0025. That is the sentence an engineer
   reads before changing the route.

The owner also required that a Knowledge Basket file reach a recipient only when it was
explicitly attached to the package. Under ADR 0025 every file A had uploaded was searchable
through any of A's packages, because an upload carries A's own grant.

## Decision

### 1. Exclusions, keyed by the document's stable identity

Migration 0024 adds `kt_package_exclusions`:

- **Key:** `(package_id, source_id, external_id)`.
- **Tenancy:** `org_id`, RLS `ENABLE` + `FORCE`, and a composite key to `kt_packages (id, org_id)`.

The key is the source and external id, not the row id. A re-sync that changes a document's text
writes a new row. An exclusion recorded against the old id would stop applying, and the excluded
thread would walk back in on the next nightly sync.

`jutsu_retrieval.search.KT_PACKAGE_PREDICATE` is the package rule minus this table. It is the one
condition every KT read composes:

- the vector scan (`search_subject_chunks`);
- the citation door (`fetch_subject_evidence`);
- every statement built from `KtScope.conditions`: the documents list and reader, claims, counts,
  coverage, bookmarks, conversation replay and the handover report.

An excluded document's claims leave with it, because every claim joins its evidence document
under that predicate. The change takes effect on the recipient's next request; nothing is cached.

**Who curates.** `kt:manage`, or the package's own subject (`jutsu_api.kt.curation_scope`). That
is the rule attaching a file already follows (ADR 0021). Anybody else, recipient included, gets
the same 404 as an unknown package.

**What a curator does:**

| Route | Effect |
|---|---|
| `GET /v1/kt/{package_id}/contents` | Lists every document the rule covers — title, source system, date, whether it is an attached file, whether it is excluded. Never a passage. |
| `POST /v1/kt/{package_id}/exclusions` | Keeps one document back. |
| `DELETE /v1/kt/{package_id}/exclusions/{document_id}` | Puts it back. |

- **Only narrowing.** A document outside the rule is a 404.
- **Closed packages.** Withdrawal stays allowed on a closed package; re-inclusion is refused
  (409), because it widens.
- **Audit.** The first page of every review writes `kt.contents_reviewed`. Each change writes
  `kt.document_excluded` or `kt.document_included`, with an opaque document id and no title.

### 2. A basket file joins a package by attachment, and only then

`KT_PACKAGE_RULE`:

```
SUBJECT_PREDICATE
AND (
      (not from the basket source AND inside the package period)
   OR (a live kt_package_files row attaches this basket file to THIS package,
       and the file is not deleted)
)
```

- **The subject's own grant is still required.** Attaching cannot reach another person's file.
- **The attachment is re-read on every request.** Detaching or deleting a file removes it from
  search, the documents tab and citations at once.
- **An attached file is exempt from the period.** A package's period ends when it is created, and
  the handover files a leaver uploads afterwards are exactly the ones it exists for. A date does
  not overrule an explicit attachment.

**Keeping back an attached file's document withdraws the file as well.** The recipient's Files
tab and its download read `kt_package_files`, not `documents`, so they carry the same exclusion
inside their own statements (`kt_files._KEPT_BACK`). A kept-back file is not listed, and its
download is the same 404 as a detached one. The handover report lists shared files through that
same reader.

### 3. Whole history is a stated choice

`KtCreatePayload.whole_history`:

- Omitting `period_days` without `whole_history: true` is a 422.
- Sending both is a 422.
- The wizard defaults to three months and asks for a confirmation before it sends the whole
  history.

### 4. The review happens before the ID is shared

The admin console shows the package's contents, with exclude and restore controls, in two places:
the panel that displays a newly created KT ID, and every package's details.

### 5. The descriptions tell the truth

`POST /v1/kt`, the web client and the admin page now say what a package grants: its recipient can
read the subject's documents while the package stays open.

## Consequences

**Curators see the titles of another employee's documents.** An HR Admin holds `kt:manage`, so the
review shows them what A's package would disclose: titles, sources and dates.

- That is strictly less than the package gives C, and it is what excluding requires.
- The alternative, subject-only curation, fails exactly when the leaver has already gone — ADR
  0025's own reason.
- Each review is an audit row. A tenant's DPIA must list it beside the package itself.

**Existing packages change on deploy.** Unattached basket files leave every existing package, and
recipients stop finding them. No other document moves: no exclusion exists yet.

**API clients that omitted `period_days` now get a 422.** The web client is updated in the same
change, and there is no other client.

**Cost.** A KT scan pays two more correlated probes per candidate document: the basket-source
check, and the exclusion probe, which the unique index serves. Both are indexed lookups inside the
documents `EXISTS`, beside the existing predicate. The two measured performance cliffs are
unchanged, and shape tests pin them:

- `chunks` stays alone in the scan's FROM;
- distance stays alone in its ORDER BY.

**Residue.**

- **There is no draft state.** A package is claimable the moment it exists, so a curator who
  shares the ID before reviewing has not reviewed. A gate on claiming would change every flow that
  creates and opens a package in one step, so it is not built.
- **Exclusion is per document.** There is no "exclude this thread", "exclude this sender" or
  "exclude this source" yet.

## Alternatives considered

**An allowlist, where a package carries only documents a curator picked.** This is the strongest
privacy answer. Rejected for ADR 0025's reason: a leaver has often gone, an admin cannot know what
matters, and an empty package transfers nothing. Exclusion keeps the rule and removes what must
not travel.

**Exclude by document id.** Rejected: a re-sync supersedes the row, and the exclusion silently
lapses.

**Subject-only curation, so no administrator sees titles.** Rejected; see Consequences.

**Keep basket files in by ownership, and let exclusions remove them.** Rejected by the owner's
requirement, and on its merits. Uploading a file is not agreeing to hand it over, and ADR 0021
already built the explicit act.
