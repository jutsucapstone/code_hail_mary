# ADR 0021 — Package-scoped Knowledge Basket sharing

**Status:** accepted
**Date:** 2026-09-08
**Related:** ADR 0010 (ACL principals are provider subjects), ADR 0011 (ACL-filtered
retrieval), ADR 0016 (KT console), ADR 0020 (Knowledge Basket object storage)

## Context

A Knowledge Basket file is granted to exactly one principal: `basket:{owner_user_id}`,
minted by `BasketConnector._grant`. That is the whole grant, floor and ceiling, for the
reason `owner_acl` gives — a grant may only name a principal the system can prove, and
the only provable principal for an uploaded file is the person who uploaded it.

The consequence, verified rather than assumed: **a knowledge-transfer recipient sees none
of it.** `routers/kt.py` resolves `scoped_acl_principals(session, user_id=principal
.user_id)` — the *recipient's* principals — and its own docstring says "The package
contributes the period; it grants nothing." So the departing employee's uploaded notes,
runbooks and recordings are invisible in the one place a handover happens.

That is fail-closed and correct as far as it goes. It is also the feature not existing.
Somebody leaving is asked to put what they know into a basket, and the colleague
inheriting their work cannot open any of it.

Three ways to close the gap were considered.

**Widen the basket grant to the recipient.** Write `document_acl` rows naming the
recipient's principals for every one of the subject's basket documents when a package is
created. Rejected. It makes the whole basket visible, not a chosen part of it; the grant
outlives the package unless something remembers to delete it; and it leaks out of the KT
console entirely — those documents would answer the recipient's ordinary `/v1/ask`
queries for ever, which is not what anyone agreed to when they attached one file to one
handover.

**Give the package its own ACL principal.** Mint `kt:{package_id}` as a principal, grant
the basket documents to it, and add it to the recipient's principal set while a package
is open. Rejected, and this one is worth stating carefully because it is the elegant
answer. It fails on ADR 0010's rule that a principal is a *provable identity in a source
system*. `kt:{package_id}` is not an identity; it is a capability wearing a principal's
clothes. Adding it to `scoped_acl_principals` would put a capability into the one
function whose entire contract is "who is this person, according to the providers" — and
the next person to add a capability there would have precedent. It would also silently
make attached files searchable, which is a different decision from making them readable.

**Reference the files from the package and re-decide access at read time.** Accepted.

## Decision

**A KT package holds references to specific basket files. The reference is not a grant;
the grant is computed on every request from the package's live state.**

```
kt_package_files
  package_id ──────► kt_packages (id, org_id)
  basket_file_id ──► basket_files (id, org_id)
  attached_by, attached_at, detached_by, detached_at
```

Nothing in that table says "may read". It says "this file was attached to this package".
Whether the caller may read it is decided fresh, per request, by `_open_for` — the
function CLAUDE.md already names as *the* KT session:

> `_open_for` is the KT session. Binding, expiry and revocation are re-decided on every
> KT request from the cookie principal plus the code; there is no session table to
> invalidate and none should be added.

So the authorization chain for reading a shared file is exactly:

```
recipient's cookie principal
  + the KT code they supplied
  → _open_for  (org via RLS · binding · revoked · completed · expired · KT_OPEN budget)
  → the package row
  → join kt_package_files on that package id, detached_at IS NULL
  → join basket_files on (id, org_id), deleted_at IS NULL
  → only then mint a signed URL
```

**Five consequences follow, and each is the reason for the design.**

*Lifecycle needs no revocation step.* Revoking, completing or expiring a package closes
access to its files in the same instant it closes everything else, because `_open_for`
refuses and nothing downstream runs. There is no grant row to forget, no sweeper to write,
no window where a revoked package still serves bytes. Detaching one file is the narrower
act and gets its own two columns.

*The recipient never gains general access.* A file not attached to the package is not in
the join, so it does not exist as far as the recipient is concerned — the same 404 they
would get for a file in another tenant. The basket as a whole is never listed to them.

*Retrieval is untouched.* No `document_acl` row is written, `scoped_acl_principals`
gains no member, and `ACL_PREDICATE` is not modified. Attached files are **readable in
the KT console — listed, previewed, downloaded — and are deliberately NOT searchable**.
Ask KT continues to answer only from what the recipient could already read. Making them
searchable is a separate decision with a different blast radius (it would put the
subject's private text into an LLM prompt assembled for someone else), and it is a
non-goal here rather than an oversight.

*The employee remains the owner.* `basket_files.owner_user_id` is unchanged, the ACL grant
is unchanged, and the owner can still rename or delete. Deleting an attached file removes
it from the package too, because the join requires `deleted_at IS NULL` — the owner's
control over their own file outranks the attachment.

*Cross-tenant access stays structurally impossible.* Both foreign keys are composite on
`(id, org_id)`, so a row cannot reference a package in one tenant and a file in another —
the database refuses it, rather than a predicate remembering to check. `kt_package_files`
carries its own `org_id` with RLS `ENABLE` + `FORCE` and the standard policy, exactly as
ADR 0002 requires.

### Who may attach

Attaching is `kt:manage`, the permission that already owns package creation — or the
package's own `subject_user_id`, since the departing employee curating their own handover
is the same act by the person whose files they are.

Two further bounds, both checked in SQL:

* the file must be owned by the package's `subject_user_id`. A handover is one person's
  knowledge; attaching a third party's file to it would be a way to launder access to
  somebody who is not leaving.
* the actor must already be able to see the file under `basket._visible_to` — their own
  file, or any file if they hold `basket:manage`. Attaching is not a way to reach a file
  you could not otherwise reach.

An HR Admin holds `kt:manage` but not `basket:manage`, so they can create the package and
cannot attach the subject's files. That is deliberate and it is the least-privilege
answer: seeing an employee's private uploads is `basket:manage`, which the organisation
grants to Owner, Super Admin and IT Admin. The alternative — letting `kt:manage` imply a
read over anyone's basket — would widen a permission to make a screen work, which §17
forbids in as many words.

### Why not a expiry column on the attachment

`kt_package_files` has no `expires_at`. The package has one, and a second expiry that
could disagree with it is a bug waiting to be written. The same argument retires a
`status` column: the package's status is the status.

## Consequences

**Good.** Access is bounded by a lifecycle that already exists and is already tested. No
new session, no new principal, no new sweeper, no cache to invalidate. The blast radius
of the feature is one join. A revoked package stops serving files with no code that runs
at revocation time.

**Costs.** Every shared-file read pays `_open_for`, including its `KT_OPEN` budget spend —
which is the point, but it means listing twenty files is one budgeted call, not twenty
(the list endpoint opens once and joins once). Attached files are not searchable, so a
recipient must know a file exists to read it; the console lists them, which is how they
know. And an attachment row survives its file's deletion as an audit fact while ceasing
to be readable, which means the count of attachments is not the count of readable files —
the API returns the readable set and the difference is invisible to the recipient by
design.

**The residue.** The departing employee cannot yet browse *their own* packages to curate
one; only a `kt:manage` holder reaches the attach endpoint through a UI. The permission
model already allows the subject, so this is a missing screen rather than a missing
decision.
