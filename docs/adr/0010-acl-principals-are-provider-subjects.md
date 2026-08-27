# ADR 0010 — ACL principals are provider-native subjects, not email addresses

- **Status:** accepted
- **Date:** 2026-08-28
- **Slice:** S6.5
- **Supersedes:** ADR 0008 decision 2 (in part)

## The two sentences this ADR exists to make true

> **Email is not an authorization identity.**
> **A source identity is a provider-native immutable subject.**

## Context

`document_acl.principal_id` had been documented since migration 0001 as matching
`users.external_id`. Investigating that before building S7's retrieval filter found three
defects, compounding.

**`users.external_id` is never written.** `grep -rn "external_id" apps/api/src/` returns
nothing. Registration and invitation both leave it `NULL`. Migration 0002 made that
deliberate and fail-closed — *"a pilot user has no IdP principal yet, so NULL means
matches no ACL grant"* — which is a correct default and not an authorization model.

**S3 emitted email addresses.** The local connector derived grants from mail participants,
which are addresses. Even with `external_id` populated they would never match an IdP
subject.

**One column cannot hold six identities.** This is the defect that forces a schema change,
and ADR 0008 understated it. A JUTSU user simultaneously *is*:

| Provider | Immutable subject |
|---|---|
| Google Workspace | OIDC `sub` / Directory user id |
| Microsoft 365 / SharePoint / Teams | Entra `oid` (GUID), per tenant |
| Slack | `U01ABCDEF`, per workspace |
| GitHub | numeric user id / node id |
| Jira / Confluence | Atlassian `accountId` — email was **removed** from their APIs for GDPR |
| Email corpus | the address itself; there is no IdP |

The mismatch was never "email versus subject". It was **cardinality**.

**Why this had to land before S7.** Built on the old model, S7's adversarial suite would
pass *vacuously*: every user has `external_id = NULL`, so §17 tests 1, 5 and 6 ("sees
nothing") pass for the wrong reason, and tests 2–4 would only pass because the test seeded
the column by hand — concealing that nothing in production ever writes it. That is the
failure ADR 0003 and the inert-`pytestmark` incident are in this repository to prevent.

## Decision

**One row per (organisation, user, source system), holding the provider-native subject.**

```
users.email ───────────────────────────────▶ display and compatibility ONLY
source_identities(org_id, user_id, source_system, subject, is_active, …)
        │
        ▼  resolved eagerly, per request, inside the session's org scope
Principal.acl_principals = {'slack:U01ABC', 'local:ada@example.com', …}
Principal.acl_groups     = {'slack:S-ENGINEERING', …}
        │
        ▼  §12 filter
document_acl.principal_id = '{source_system}:{subject}'
```

### 1 · Subjects, stored verbatim, resolved late

Resolving provider → JUTSU at *ingestion* was rejected, and the reason is not performance.
A document's ACL routinely names people who have no JUTSU user yet — not onboarded, or
external collaborators. Resolving at ingest would silently **drop** those grants, and
onboarding the person later would not restore access without re-ingesting every document.
§4.5 inherits source ACLs; storing our interpretation instead of the source's statement
loses fidelity, and §4.4's supersede discipline says derived facts point at evidence
rather than replace it.

### 2 · Namespaced, and `document_acl` is untouched

`principal_id` becomes `{source_system}:{subject}`. A **data convention, not DDL**: the
column, the primary key and the `(principal_id, document_id)` index are all unchanged.
§12's filter changes only from `= $3` to `= ANY($3)`.

Denormalising `source_system` onto `document_acl` — the ADR 0002 precedent — was rejected:
it forces an awkward composite `ANY`, changes the primary key, and buys nothing a string
prefix does not already give. The prefix makes a Slack member id incapable of matching a
GitHub grant, which is the property that matters.

### 3 · Uniqueness is scoped to the organisation

`unique(org_id, source_system, subject)`, not global. Two tenants may legitimately connect
the same Slack workspace, and a global constraint would make the second one's link fail.

### 4 · Revocation is a flag, resolution is eager and uncached

`is_active` is the switch; offboarding flips it rather than deleting, so the audit trail
survives. Resolution reads the database on **every request** — §17 test 3 requires a group
removal to take effect on the next query with no cache flush, and the same must hold for a
revoked identity. A cache that outlived a revocation would be an authorization decision
made from stale state, which is the hardest failure to notice and the worst to explain.
One indexed read per request is the right price.

### 5 · The organisation is never an argument

`scoped_acl_principals(session, *, user_id)` takes no org id. The tenant comes from the
GUC that `scoped_role` set from the *session-derived* org, so row-level security scopes
both reads and no request parameter can widen the scope. A test asserts the signature,
because an `org_id` parameter appearing there later is exactly the regression to catch.

## Consequences

- **An email change is no longer a security event.** Previously it would have stranded
  every `document_acl` row naming the old address — access silently gone, nothing
  reported. Now `users.email` changes and authorization is untouched.
- **Offboarding and revocation are one row**, not an ACL rewrite.
- **GDPR / DPDP erasure becomes tractable** (§17). Addresses in `document_acl` put personal
  data in a table replicated per document; erasing a subject would mean rewriting millions
  of rows. With subjects, erasure deletes the `source_identities` rows and the remaining
  ACL rows hold an opaque string that maps to nobody. Atlassian removed email from their
  APIs for precisely this reason.
- **`users.external_id` keeps its column and loses its meaning.** It is not dropped: it is
  still the natural home for a primary IdP subject if SSO lands, and dropping a column is
  irreversible in a way this decision does not require. Nothing in the authorization path
  reads it, and `jutsu_core/ids.py` and `rbac.py` say so.
- **`user_groups` gained `org_id` and RLS.** It feeds the group half of the §12 filter,
  which made it an authorization input the database was not isolating. Group values are
  stored already namespaced, in the same form as `principal_id`, for the same reason.
- **Fail-closed is unchanged and now explicit.** No identity → empty principal set →
  `= ANY('{}')` → no rows. Previously the same outcome arrived via a NULL nobody wrote.

## What this does not solve

- **The graph side.** §7 `Person` nodes carry `email`, and §4.6 requires a fact whose
  evidence is entirely ACL-invisible to be invisible. Graph evidence filtering will need
  the same subject resolution; nothing here does it.
- ~~**Nothing populates `source_identities` in production yet.**~~ **Closed by S6.6
  (2026-08-28).** Registration and invitation acceptance now create `local:{verified_email}`
  inside the transaction that creates the user, and administrators may link arbitrary
  provider subjects through four guarded routes. See *Addendum* below.
- **`pii_vault` still has no RLS** despite carrying `org_id`. Found while enumerating; a
  separate defect and deliberately out of this slice.

---

## Addendum — S6.6, the lifecycle (2026-08-28)

The decision above said what a source identity *is*. This says who may create one, and the
answer is shaped entirely by one sentence: **linking a source identity is granting document
access.**

### Two writers, not equally trusted

**Automatic, from a proven mailbox.** Registration redeems an OTP; invitation acceptance
consumes a single-use token that reached one address and nowhere else. Both already prove
mailbox control before they create a user, and for `SourceSystem.LOCAL` the subject
namespace *is* email — so `local:{verified_email}` is a mapping JUTSU has proven rather
than one it assumed. The address passed is the same value written to `users.email`, never a
request field. `/v1/invitations/accept` carries a free-text `full_name`; if that string
could reach the subject, accepting an invitation would be a way to claim any principal in
the tenant.

**Administrative, deliberately and audibly.** Any other subject — a Slack member id, an
Atlassian `accountId` — is a claim nobody verified, so it needs `integration:connect`, a
rank check, and an audit row.

### An administrator may not link themselves

Unconditional, and **not a permission check**. §17 divides the world: roles gate features,
ACLs gate data, and nothing in `Permission` may confer a document read. An IT Admin who
could link themselves to a colleague's Slack subject would read that colleague's documents
with no ACL ever consulted — a role conferring data access, which is the exact property the
separation exists to deny. Making it a permission would make it no refusal at all for an
Owner, who holds every permission there is.

Self-*revocation* is allowed. Removing your own access is not an escalation, and refusing it
would leave somebody unable to undo a link they no longer want.

### Rank, tenant, duplicate

`outranks` is strict, so a peer is refused as firmly as a superior — linking somebody's
identity is acting on their access, and it inherits the ceiling that stops an invitation
conferring a higher role. The target is resolved under row-level security, so a user in
another organisation is `NotFound` rather than `PermissionDenied`: the caller learns nothing
about whether the id exists somewhere they cannot see. A subject already claimed in the
tenant raises `Conflict` rather than re-pointing — moving a subject between people is a
transfer of access and must be an explicit revoke-then-link.

Automatic linking hits the same uniqueness rule and, being `ON CONFLICT DO NOTHING`, resolves
it by linking **nothing**: a new user whose address is already held by somebody else holds no
local principal. Idempotent for the retry case, fails closed for the collision case.

### What is still not solved

- **No OAuth, no provider tokens, no credential storage.** Phase 4. A provider flow becomes
  a new *source* of subjects feeding this same table, not a second lifecycle.
- **`revoke_all_for_user` has no production caller.** There is no employee-deactivation
  route today. The route that lands it must call this rather than reimplementing the loop,
  which is why the audit rows are written in the primitive.
- **The graph side is untouched**, exactly as the original decision recorded.
- **`pii_vault` still has no RLS.** Unchanged, still a separate defect.
