# ADR 0025 — A knowledge-transfer package reads its subject's knowledge

**Status:** accepted
**Date:** 2026-09-14
**Supersedes:** migration 0013's "a package never widens authorization" rule; ADR 0016 §3
(KT retrieval under the recipient's own ACL); ADR 0021's "attached files are not
searchable" non-goal, for files the subject owns.
**Related:** ADR 0010 (principals are provider subjects), ADR 0011 (ACL-filtered
retrieval), ADR 0016 (KT console), ADR 0021 (package-scoped basket sharing)

## Context

Reported: employee B claims a package about employee A, opens Ask KT, asks what A was
working on — and is shown B's own projects.

A trace of the request path found no retrieval-context bug. It found the design working
exactly as written. Every KT read — the copilot, the documents list and reader, the
knowledge tabs, their counts, the handover summary, the workspace, bookmarks and
conversation replay — authorized evidence with `ACL_PREDICATE` bound to **B's**
principals, narrowed only by the package's period. `subject_user_id` appeared in no
retrieval, document, claim or summary query. Three records said so on purpose:

* migration 0013: *"What a recipient can read inside the KT workspace is decided by THEIR
  OWN grants… A package narrows presentation (period, scope); it never widens
  authorization."* — and `kt:open`'s description: *"Grants no document."*
* ADR 0016 §3: KT retrieval is `search_chunks` with the package window ANDed inside the
  ACL predicate — *"intersection, never union"*;
* ADR 0021: the subject's attached files are *"deliberately NOT searchable"*, because that
  *"would put the subject's private text into an LLM prompt assembled for someone else"*.

The consequence is the reported symptom in a real tenant. Connectors grant each document
to its account owner and nobody else (`owner_acl`), so B can read almost none of A's
material, and "everything B may read inside A's period" is B's own corpus. The KT console
could not transfer knowledge, which is the only thing it is for.

Showing B A's knowledge is therefore an authorization change, not a fix to an existing
authorization. The request that reported the defect also forbade redesigning KT
authorization; the two could not both be honoured. The owner was asked and chose between
three options — (1) the subject's own accounts, through the package; (2) only files
explicitly attached to the package; (3) no authorization change, narrowing B's own ACL to
documents involving A. **They chose (1).** This ADR records it.

## Decision

**An opened package is a read capability over its subject's own documents, bounded by the
package's categories and period, re-decided on every request.**

### 1. One predicate, and it is not a wider copy of the caller's

```sql
EXISTS (SELECT 1 FROM document_acl a
        WHERE a.document_id = d.id AND a.permission = 'read'
          AND a.principal_type = 'user'
          AND a.principal_id = ANY(:subject_principals))
```

`SUBJECT_PREDICATE`, a module constant beside `ACL_PREDICATE` in `jutsu_retrieval.search`.
It asks a different question — *is this document the subject's own* — which is why it has
one arm:

* **no `group` arm.** A group grant is a team's material. Passing it would hand B every
  document shared with every team A sat in — a much wider decision than a handover.
* **no `org` arm.** An organisation grant is everybody's; B can already search it from
  their own account.
* **`user` only.** It is the grant every connector writes for the account owner and the
  Knowledge Basket writes for the uploader (`basket:{user_id}` is a linked identity), so it
  names precisely A's connected accounts and A's basket.

The bind is `:subject_principals`, never `:principals`, so the two predicates cannot be fed
each other's set and a swap fails to bind instead of authorizing the wrong person.

### 2. Requester, subject and package are three different things

```
requester   B        authenticated; `_open_for` decides B may hold this package
subject     A        whose knowledge; resolved from the package row, never a parameter
package     scope    categories and period; binding, revocation, completion, expiry
```

`jutsu_api.kt.KtScope` carries the subject, their principals, the categories and the
window. It is **built only by `_scope_for`, from the row `_open_for` returned** — asserted by
AST in `test_kt_subject_scope.py`, because Python cannot make a constructor private and a
second construction site would pass every behavioural test until it was used. Every KT
reader takes a `KtScope`; no KT reader and no KT route resolves the requester's principals
at all. The requester's corpus is not an input to any KT read, so it cannot leak into one.

`KtScope.conditions()` is the single SQL fragment — subject predicate, supersession, period —
that every KT statement composes. Ask KT, the tabs, the counts, the workspace, bookmarks,
conversation replay and the handover report cannot disagree about what a package holds.

### 3. Retrieval keeps its shape and its guarantees

`search_subject_chunks(subject_user_id, …)` is `search_chunks`' scan, window, cursor and
escalation ladder with the authorization conjunct swapped — a test asserts the statement is
the caller's statement with exactly that substitution. Both measured performance cliffs
hold (chunks alone in the FROM; distance alone in the inner ORDER BY). Neither function
takes a principal set, a group set, an org id or a requester id. `fetch_subject_evidence` is
the matching citation door, with the same window.

### 4. Categories gate raw passages

A package without `documents` exposes no raw passage anywhere: the documents list, the
reader, **Ask KT** and the KT evidence route all refuse with one sentence. Claim categories
gate claim types exactly as before. This is new for the copilot, which previously ignored
scope because it read the requester's own corpus.

### 5. Active identities only

`jutsu_db.acl.resolve_subject_principals` reuses `resolve_acl_principals` and keeps its
`is_active` rule, so an identity revoked because it was linked wrongly stops contributing
documents everywhere, a handover included.

### 6. A KT citation door

`GET /v1/kt/{code}/evidence/{chunk_id}`. The generic `/v1/evidence` answers to the
requester's own ACL and would call every KT citation absent. The KT door runs `_open_for`,
the `documents` category, then the subject predicate and the window; everything outside is
the same 404 as a chunk that never existed. The console's copilot citations and knowledge
cards use it.

### 7. The handover report shares the boundary

`POST /v1/kt/{code}/handover-report` composes the first-day summary and renders it as a PDF
in one request — one `_open_for`, one claims read shared by the narrative and the sections,
one model call through the existing provider chain and citation gate, rendered in memory
and never stored. It accepts no content from the browser: a PDF headed "Knowledge Transfer —
Handover Summary" that printed text a client sent would be a forgery kit. Sections are the
extracted claims with their verbatim evidence; only the executive overview is model-written.

### 8. Logs carry counts, never the code

Five events — `kt_retrieval_context_created`, `kt_search_started`, `kt_search_completed`,
`kt_summary_started`, `kt_summary_completed` — carry the package id, counts, flags and
timings, beside the request context every line already has. No KT code, address, question,
passage, title or answer: a test captures a whole create–claim–ask–report–refusal flow and
reads back the lines as the handler renders them. The code travels in the recipient routes'
path, which uvicorn's access line and the error handlers log, so
`jutsu_core.logs.RedactKtCode` scrubs it from every line. Cloud Run's own request log is
written outside the container and still holds it.

## Consequences

**A's private material from the package period becomes readable to B.** Mail, chat,
documents and basket files from A's own connected accounts, within the categories and the
period the administrator chose, while the package is open. That is the purpose of the
feature and it is a real privacy decision: masked text removes addresses, phones, cards and
IBANs, and **does not remove names** (CLAUDE.md), so B reads names in A's correspondence.
The controls are the package's own — `kt:manage` to create one (Owner, Super Admin, HR Admin),
category and period chosen at creation and immutable afterwards, recipient binding, expiry,
revocation and completion re-checked on every request, and an audit row for every open,
question and report. A tenant's DPIA must describe this.

**It stops the moment the package does.** No grant row is written, `document_acl` is
untouched, `scoped_acl_principals` gains no member and B's ordinary `/v1/search`, `/v1/ask`
and `/v1/evidence` are unchanged — asserted by test. Revoking, completing or expiring a
package closes every KT read on the next request, with nothing running at revocation time.

**The residue: deactivating a leaver's identities before their handover empties the
package.** Fail-closed, and the order an offboarding runbook must respect: hand over, then
deactivate. (`revoke_all_for_user` has no production caller today, so nothing does this yet.)

**A package without `documents` has no copilot.** The copilot reads raw passages. Grounding
it on claims alone for such packages is possible and is not built.

**Existing tests that pinned the old contract were rewritten, not loosened.** Each now seeds
evidence under the subject and adds an explicit assertion that the recipient's own document
is absent. The "recipient with no grants gets an empty page" test became its real
counterpart: a subject with no active identity gets an empty page, never a fallback.

**Performance.** One extra indexed read per KT request (the subject's identities). The
workspace and the report open the package once rather than once per panel, which spends
fewer `KT_OPEN` allowances than before.

## Alternatives considered

**Only files attached to the package (ADR 0021's references, made searchable).** The
narrowest widening, and the attachment act is explicit consent. Rejected by the owner: a
departing employee has often already left, an HR Admin cannot attach (that needs
`basket:manage`), and a package would be empty unless somebody curated it.

**No authorization change — B's own ACL narrowed to documents involving A.** Removes the
contamination and cannot leak. Rejected by the owner: A's own accounts stay invisible, so
nearly every "what was A working on" question answers "not enough evidence".

**`ACL_PREDICATE` evaluated against A's principals.** Rejected here: it carries the group and
org arms, handing B A's entire reach — every team space and the whole tenant — not A's
knowledge.

**A package principal (`kt:{package_id}`) in the recipient's principal set.** Already
rejected by ADR 0021 for the reason that still holds: a capability is not a provable
identity, and `scoped_acl_principals` is the one function whose contract is identity.
