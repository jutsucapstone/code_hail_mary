# 0014 — A connection links its proven subject, and that is the whole grant

Status: accepted (S10)

## Context

Migration 0012 stored the OAuth-proven `provider_subject` on the connection row
"granting nothing", and deferred the question it raises: when an employee connects
Google Drive, what makes the documents that sync from it *visible to them*?

The retrieval path answers visibility one way only (ADR 0010, ADR 0011): a document
carries ACL rows naming namespaced principals `{source_system}:{subject}`, and a
caller holds the principals their `source_identities` rows resolve to, eagerly, per
query. Until something bridged connections to identities, content fetched under an
employee's own token would be invisible to that same employee — an ACL row naming
`slack:U0AB12CD` matches nothing when nobody holds that principal.

Two existing rules box the design in, correctly:

1. **An administrator may never link a subject to themselves**, and the refusal is not
   a permission check (§17). The danger it guards is an admin *asserting* an identity:
   nothing but their word connects them to the subject, and every permission they hold
   should not make their word sufficient.
2. **An automatic link takes its value from a verification, never from a request.**
   Registration links the OTP-verified address; invitation acceptance links the address
   the token reached.

## Decision

**The OAuth callback links the proven subject as a source identity, in the provider's
declared ACL namespace, in the same transaction that stores the credential.**

The callback is a verification in exactly the sense rule 2 demands: the subject comes
from the provider's identity endpoint, reached with a token minted seconds earlier by
an exchange whose PKCE verifier and single-use state never left the server. Nobody
asserted anything; the provider answered. This is `link_verified_email` in the other
namespace direction, and it carries `linked_by = 'oauth_connection'` so the row says
how it came to exist.

### The namespace mapping

`source_identities.source_system` has seven values; the catalogue has eleven
providers. The mapping is declared per provider (`Provider.acl_namespace` in
`jutsu_core.providers`), not inferred:

| Providers | Namespace | Subject |
|---|---|---|
| google_drive, gmail, google_calendar, google_meet | `gmail` | OIDC `sub` — one Google account, one subject, shared by all four products |
| onedrive, teams, sharepoint | `m365` | Graph `id` (the directory object id) |
| slack | `slack` | `auth.test` `user_id` |
| jira | `jira` | Atlassian `account_id` |
| confluence | `confluence` | Atlassian `account_id` |
| github | `github` | GitHub numeric `id` |

Jira and Confluence share an Atlassian account id but keep separate namespaces: the
enum separates them, fetchers stamp ACLs inside their own namespace, and collapsing
them is a migration that can happen later without loss — the reverse is not true.

### Fail-closed on conflict, same as email

`ON CONFLICT DO NOTHING` on `(org_id, source_system, subject)`: a subject already held
by a different user in the tenant links nothing and nobody's access moves. The person
keeps a working connection — content fetched under their token still ingests — but
gains no visibility until an administrator resolves who the subject belongs to. One
subject is one person per tenant.

### Disconnecting is not unlinking

Disconnect (and administrative revocation of a connection) deletes the credential and
stops the syncing. It deliberately does **not** revoke the identity: who somebody is
did not change when the pipe closed, and documents already ingested that they could
read yesterday are documents they can read today. Removing *visibility* remains what
it always was — identity revocation, an explicit administrative act with its own rank
check and audit trail. The two levers answer different questions and stay separate.

## Consequences

* Content fetched by a provider sync under an employee's token is visible to that
  employee the moment extraction of the ACL names their subject — no admin step, no
  cache to flush (resolution stays eager per ADR 0010).
* Provider fetchers must stamp ACL principals in the connection's declared namespace
  with provider-native subjects. Granting wider than the connecting user's own subject
  requires provider-side ACL data mapped to *subjects* (not emails), and until a
  fetcher can prove that mapping it must not guess it — the connecting user's subject
  is the floor and, for now, the ceiling.
* A tenant where two people claim one provider account surfaces as a silent non-link,
  exactly like the email path. The admin identities surface is where it gets resolved,
  on purpose.
