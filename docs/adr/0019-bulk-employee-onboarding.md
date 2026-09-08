# ADR 0019 — Bulk employee onboarding

**Status:** accepted
**Date:** 2026-09-07
**Supersedes:** nothing
**Related:** ADR 0010 (identity and ACL principals), migration 0002 (`uq_invitations_org_email_live`)

## Context

Onboarding was one address at a time. A firm admitting a graduate intake, or moving an
existing team onto JUTSU, had to type eighty addresses into a one-line form and watch for
the six that failed — and the failures are not rare: a re-pasted roster is full of people
who already have accounts, addresses that were mistyped in the source system, and people
whose invitation is still outstanding from last week.

Three things made this more than a loop.

**One bad row can silently discard the whole batch.** Postgres aborts a transaction at its
first error. `invite_employee` can raise `IntegrityError` from its INSERT — the partial
unique index refuses a second live invitation for one address — and after that every
statement on the connection fails until the transaction ends. A naive loop would therefore
invite the people before the failure, invite nobody after it, and report success. This is
the same trap the ingestion worker documents for `record_failure`, in a place where the
consequence is people who were told they were invited and never were.

**A second insert path is a second place for authorization to be wrong.** `invite_employee`
owns the rank ceiling (`outranks`), the already-a-member refusal, the org-less token index
that acceptance depends on, the audit row and the mail. A bulk path that wrote
`invitations` directly and forgot `outranks` would be a privilege escalation with a CSV
attached — and nothing in the type system would notice.

**Delivery does not fit in a request.** `SmtpEmailSender` opens a fresh connection per
message: connect, STARTTLS, LOGIN, DATA. Two hundred of those in sequence is close to
three minutes.

## Decision

**Two routes, and the split is the product.**
`POST /v1/employees/invitations/preview` writes nothing, sends nothing, and classifies
every row: `ready`, `already_member`, `already_invited`, `duplicate`, `invalid_email`,
`invalid_role`, `role_too_high`. `POST /v1/employees/invitations/bulk` acts on rows the
administrator approved. Both take `member:invite` — the same permission as inviting one
person, because inviting eighty people is inviting one person eighty times, and a separate
higher permission would either lock administrators out of a tool they are already entitled
to use or become a reason to hand somebody a broader role.

**The preview is advice; the send re-decides.** The rows come back from the browser, so
`invite_many` re-runs `classify` and `invite_employee` re-runs `outranks` per row. A client
that skipped the preview, or edited its result, meets exactly the same refusals.

**Every invitation still goes through `invite_employee`.** The bulk module parses, groups
and reports; it never writes an `invitations` row.

**One savepoint per row.** `session.begin_nested()` around each call, so a row that fails
at the database rolls back to its own savepoint and the connection stays usable.
`test_a_row_that_aborts_the_transaction_does_not_take_the_batch_with_it` induces a real
Postgres error rather than a Python one, because only a database error reaches the failure
mode the savepoint exists for — a bare `raise` leaves the connection perfectly healthy and
the test would pass with the savepoints removed.

**Delivery is lifted out of the savepoint and batched.** Each row is given a collecting
transport; the messages are delivered afterwards, five at a time. Five rather than fifty
because submission endpoints throttle concurrent logins, and a bulk import that trips the
provider's throttle fails in the way that is hardest to explain.

**A message that cannot be delivered revokes its invitation.** The savepoint is released by
then, so the row cannot be rolled back — but revoking reaches the same end state (no live
invitation, no usable token) and, because `uq_invitations_org_email_live` is partial on
exactly `accepted_at IS NULL AND revoked_at IS NULL`, it is also what lets the
administrator retry that address.

**`openpyxl` is a dependency, and `python-multipart` is not.** A `.xlsx` is a zip of XML
and cannot be read as text, so the alternative was telling every administrator to re-save
their file as CSV. It is read bounded and read-only: refused above 1 MB *before* the parser
sees it, first worksheet only, 2 000 rows, `read_only=True` to stream rather than
materialise (a small zip decompresses into a very large sheet), `data_only=True` so a
formula's stored result is taken rather than evaluated. The workbook arrives base64-encoded
in the JSON body and a CSV arrives as text, so no multipart handler exists anywhere in the
API and the generated TypeScript client stays honest.

## Consequences

**A batch is bounded at 200 addresses.** A larger import is several batches, which is also
how an administrator wants to review one.

**Re-sending the same list is the retry.** Anybody who already received mail comes back as
`already_invited` rather than a second message, so the button can be pressed again safely
and "retry the failures" needs no separate endpoint.

**An expired invitation is now retired on re-invite.** `uq_invitations_org_email_live`
cannot mention `expires_at` — `now()` is not immutable and no index predicate may reference
it — so an invitation that merely ran out of time still occupied the slot. Re-inviting that
person raised `IntegrityError` and answered "that person already has an invitation
waiting", which was the one thing that was not true; nobody could invite them again, ever.
`invite_employee` now revokes the expired row first. This fixes the single-invite path as
well as the bulk one.

**A failed delivery leaves a revoked row behind.** That is the audit trail of an attempt
that was made, and it is deliberate — the alternative is deleting the evidence that JUTSU
tried to email somebody.

## Rejected alternatives

**A queued job per invitation.** Correct at ten thousand addresses and wrong here: it would
need a new job kind, a migration, a worker handler and SMTP configuration on the worker,
and it would replace an answer the administrator reads immediately with a page they have to
come back to. Two hundred addresses deliver in tens of seconds.

**Parsing the spreadsheet in the browser.** Avoids the Python dependency and adds a
JavaScript one — a `.xlsx` parser is a parser wherever it runs — while moving the file
format's failure modes to the one place they cannot be tested against a real Postgres.

**A `member:bulk_invite` permission.** Least privilege is about what somebody may do, not
how many times they may do it. A permission that gates the tool but not the underlying
capability protects nothing and is a reason to over-grant.

**Sending inside each savepoint, as the single-invite path does.** Simplest, and atomic per
row — but it is the three-minute request. The compensating revoke is the price of the
batch, and it is bounded, tested and visible in the response.
