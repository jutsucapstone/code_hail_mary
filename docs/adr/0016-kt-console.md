# 0016 — The KT console: per-request authorization, a narrowing window, and what a recipient may keep

Status: accepted

Scope: the knowledge-transfer console built on migration 0013's packages — the copilot,
conversation history, bookmarks, progress, learning path, coverage and recommendations
(migration 0019, `jutsu_api.kt_workspace`, `routers/kt_console.py`, `apps/web/app/kt`).

## Context

Migration 0013 made a KT package a scoped, expiring, revocable window over one colleague's
context, and `jutsu_api.kt._open_for` re-decides that window on every request. What it did
not do was keep anything between requests: a question asked in the workspace lived in React
state and died with the tab; nothing recorded what a recipient had read or found unclear;
"Ask KT" searched the whole corpus rather than the package; and the door — a 40-bit code —
had no rate limit. The brief for this work asked for all of that, plus a "KT session
distinct from the URL", a "KTConversation" it believed already existed, background
generation jobs, and "GraphRAG".

An audit of the repository (24 agents, every claim adversarially verified) established what
was true before a line was written: no `KTConversation` existed in code or spec; the graph
is unpopulated and absent from production; **production runs no worker service and no
Redis**, so any enqueued job would never execute there; every session hard-expired sixty
minutes after creation because `auth.touch_session` had never been called; and the answer
layer runs on the Claude API, not the Gemini stack §5 names. Each decision below follows
from one of those facts.

## Decisions

### 1. There is no KT session table. The session is `_open_for`, on every request.

The brief's "session distinct from the URL" already exists here in a stronger form:
binding, expiry and revocation are re-decided on every KT request from the authenticated
cookie principal plus the code. A session row would be a second copy of that state, able to
drift from the first, and the thing a revocation would have to invalidate. Nothing was
added. What was fixed is the base session: `resolve_principal` now calls
`auth.touch_session` when the idle deadline has moved by `SESSION_TOUCH_INTERVAL_SECONDS`,
bounded by the absolute expiry — sliding idle expiry, as `config.py` always described it.

### 2. Binding is checked before state.

`_open_for` answered "revoked" or "expired" before asking whether the caller was the
recipient, so any member of the organisation holding a bound code learned that the package
existed and had been closed. The order is now: bound or addressed to somebody else → the
uniform 404; then revoked → 403 with its sentence; then expired → 403 with its sentence. The
right person still sees exactly why the door is closed (§39); the wrong person sees the same
404 as for a typo. Pinned by `test_kt_console.py`.

### 3. Retrieval narrows inside the ACL predicate. One function, two callers.

`routers/kt.py` recorded the decision to have one search path because a second search route
is a second place an ACL bug can live. That decision was about the *predicate*, not the URL.
`search_chunks` gained one parameter, `within: RetrievalWindow`, whose bounds are ANDed
inside the same `EXISTS (… AND {ACL_PREDICATE})` in the inner scan — intersection, never
union, so it can leave out documents the caller may read and can never let in one they may
not. The signature test still forbids `org_id`, `principals` and `groups`; the SQL-shape
tests still hold (`FROM chunks c`, no join in the scan, distance-only inner `ORDER BY`); a
new test proves a window over an unauthorized document returns nothing.
`POST /v1/kt/{code}/ask` calls that same `search_chunks` with the package's period, the
same `synthesise_answer`, and spends the same search budget.

### 4. History is context, never evidence.

Prior turns reach the model as a labelled preamble ("Conversation so far — context only,
never cite it"), never as numbered passages. `_grounded` resolves markers against the
retrieved list alone, so a marker echoed from an earlier answer is refused, and no evidence
means an immediate free refusal however rich the history. The system prompt names the rule.
Six turns, each truncated to 1 000 characters — bounded by the caller, never summarised.

### 5. Conversations store references and are closed by the package.

`kt_messages.citations_json` holds `{marker, chunk_id, document_id, title, source}` —
never passage text. Every read of a conversation re-runs the cited documents through
`ACL_PREDICATE` and marks each citation `available`; a document the caller can no longer
read renders "no longer available", not a link. Every read passes `_open_for`, so revoking
the package closes the history on the next request. The answer prose itself is what the
recipient was shown at the time; it is theirs, scoped to them by `(kt_package_id, user_id)`
under RLS, and it is the one thing here that a later ACL change cannot un-show. That
trade-off is accepted knowingly, for the recipient's own record only, and is why the
handover *summary* remains unpersisted (`kt.py` gives the reason).

### 6. One limiter, many buckets.

`search_budget` gained a `bucket` column in its key rather than a sibling table per
endpoint. `kt_claim` (10/min) makes the 40-bit code space unprobeable — the denied-open
audit rows remain the evidence, the budget is the wall — and `kt_summary` (6/min) bounds the
one paid call the console makes per press. Spends commit on their own session before the
guarded step, and a 429 now carries `Retry-After`, set once in the error handler.

### 7. Nothing is generated to fill a gap, and nothing runs in the background.

The learning path, recommendations and "still unclear" are ordered lists of real claims and
documents, each with a stated `why`, computed on demand from what the recipient may read
now. Coverage is counts plus one ratio with one formula — `chunks_covered / chunks_total`
over the latest extraction run of each readable document in the window — and it says
"cannot be calculated reliably yet" when the inputs are missing rather than printing a
number. A background generation job was not built: production has no worker service, so a
job would sit `pending` forever while looking like a feature; the synchronous, on-demand,
unpersisted model the handover summary already uses is the one that actually runs there.

### 8. People are not ranked.

The People stage lists the most recent mentions and says so. No score, no "who to contact"
ranking, no per-person figure: non-negotiables 16–18 require signals exposed to the ranked
person and a consent state, and no consent field exists anywhere in the schema. Adding one
is a migration and an ADR, not a UI feature.

### 9. Not built, and said so.

No graph leg — Neo4j is unpopulated everywhere and unconfigured in production; a "GraphRAG"
label would be false, so the copilot is pgvector plus grounded synthesis and says nothing
else. No SSE — the citation gate needs the whole answer and the Next proxy buffers bodies;
streaming post-gate events is a later, separate decision. No code rotation — it would
complicate binding for a benefit nobody has asked for.

## Consequences

* Every recipient-facing KT route calls `_open_for` first and reuses `ACL_PREDICATE` inside
  SQL; there is still exactly one place an ACL bug could live.
* New tenant tables (`kt_conversations`, `kt_messages`, `kt_bookmarks`, `kt_progress`) bind
  to `(kt_package_id, org_id)` and `(user_id, org_id)` by composite foreign key, are FORCE
  RLS, and are registered in `RLS_TABLES` so the isolation battery covers them.
* Audit gained `kt.opened`, `kt.copilot_asked` (counts only — never the question),
  `kt.bookmarked`, `kt.conversation_archived`, `kt.extended`, `kt.readdressed`, and every
  API-written KT row now carries `correlation_id` = the request id. Log lines carry
  `request_id`, `org_id` and an opaque `user_id` (`logging_context.py`).
* Four new environment variables (`KT_CLAIM_RATE_*`, `KT_SUMMARY_RATE_*`) with documented
  defaults; none is required.
* **Production blocker, unchanged by this work:** with no worker deployed, extraction never
  runs in production, so the knowledge tabs, coverage and learning path are empty there
  until a worker service (and the queue transport it needs) is deployed. The console shows
  the honest empty state and its reason rather than anything else.
