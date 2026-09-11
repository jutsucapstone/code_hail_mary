# The KT console

What a person taking over a colleague's responsibilities sees when they enter a KT ID, and
what holds it up. `docs/adr/0016-kt-console.md` argues the architecture; this page is the
operator's and developer's reference.

## The journey

```
/handover                 enter the KT ID          POST /v1/kt/claim   (kt:open, budgeted)
   ↓ 200
/kt/{code}                Overview — resume card, coverage, recommended today,
                          still unclear, the learning path in brief
/kt/{code}/learn          the full learning path with progress controls
/kt/{code}/ask            the copilot, with every conversation kept
/kt/{code}/documents      what the recipient may read inside the window
/kt/{code}/projects …     the knowledge tabs (extraction claims, quote-gated, ACL-filtered)
/kt/{code}/saved          bookmarks, private notes, saved questions
/kt/{code}/handover       the on-demand executive summary (budgeted)
```

Every one of those pages re-runs the package's authorization on the server:
`jutsu_api.kt._open_for` — binding to the recipient, expiry, revocation — on every request,
with a denied open written to the audit trail on its own transaction. There is no KT session
token. Revoking a package closes the workspace, the history, the bookmarks and the copilot
on the next request from whatever browser had it open.

## What the recipient may keep (migration 0019)

| Table | Holds | Never holds |
| --- | --- | --- |
| `kt_conversations` | one recipient's thread in one package: title (the first question), timestamps, `archived_at` | anything generated |
| `kt_messages` | the question; the answer as shown (or the refusal sentence); citations as **references** (`marker`, `chunk_id`, `document_id`, title, source) | passage text |
| `kt_bookmarks` | a saved claim / document / message / free-text question, with a private note | a copy of the item |
| `kt_progress` | `seen \| done \| unclear` against `claim:{id}` / `document:{id}` / `step:{key}` | a copy of the item |

All four carry `org_id`, are `ENABLE`+`FORCE` row-level-secured with the standard predicate,
bind to their package **and** tenant by composite foreign key (`kt_packages` gained
`UNIQUE (id, org_id)` for exactly this), and are registered in `jutsu_db.RLS_TABLES`.

On every read, a conversation's citations are run back through the ACL predicate and marked
`available`. One that no longer resolves renders as "no longer available to you" — not a link.

## The copilot

`POST /v1/kt/{code}/ask` with `{question, conversation_id?}` — nothing else. The order is the
cost control: `answers_configured()` (a free 503 without an answer model) → the search budget,
spent on its own committed session → embed the question → `search_chunks(user_id, vector,
within=RetrievalWindow(period_start, period_end))` → `synthesise_answer(evidence, history)` →
both turns stored → `kt.copilot_asked` audited with counts only.

Two properties the design rests on:

* **The window narrows inside the ACL predicate.** `RetrievalWindow` is two conjuncts on
  `d.created_at` inside the same `EXISTS (… AND ACL_PREDICATE)` in the inner scan. It can
  leave out documents the caller may read; it cannot let in one they may not. The signature
  test still forbids `org_id`, `principals` and `groups`; the SQL-shape tests still pass.
* **History is context, never evidence.** The last six turns (each truncated to 1 000
  characters) reach the model as a labelled preamble. The citation gate resolves markers
  against the retrieved passages alone, so an earlier answer cannot become a source.

The copilot is pgvector retrieval plus grounded synthesis. There is no graph leg: Neo4j is
unpopulated everywhere and unconfigured in production, and the console says nothing that
would suggest otherwise.

## Coverage, the learning path, recommendations, gaps

`GET /v1/kt/{code}/workspace` computes all of these on demand from the recipient's visible
evidence. Nothing is stored except the recipient's own progress markers.

**Coverage** is counts plus one ratio with one formula:

```
extraction_ratio = Σ chunks_covered / Σ chunks_total
```

summed over the latest finished extraction run of every document in the window the
recipient may read (`extraction_runs.stats_json`, written by the worker). It is the fraction
of readable text that could have produced a claim at all — extraction reads a prefix of long
documents and records how far it got. When there is nothing to divide (no readable
documents, or none extracted) the ratio is `null`, `reliable` is `false`, and `reason` says
which — the UI renders the sentence and no percentage. Per-category counts are the same
counts the tabs show, computed under the same predicate.

**The learning path** is stages by day — responsibilities, projects, decisions, people,
meetings, documents — each listing up to five *real* items: the most recent claims of that
type the recipient may read, and the newest readable documents in the window. Every item
carries a `why` (source · document · date). A stage whose category is out of scope, or has
nothing visible, is absent; an empty path renders "Not enough evidence yet" with the
coverage reason. People are listed by recency and the `why` says so; nobody is scored.

**Recommendations** (at most five, each with a `why`): the next path item not yet done, up
to two items marked unclear, a recent high-confidence decision not yet seen, a recent
document not yet opened.

**Still unclear** is two kinds of gap, labelled: items the recipient marked `unclear`
("you"), and categories in scope with nothing visible ("evidence"), each with the reason —
extraction has not run over the readable documents, or nothing extracted in the window is
readable by this account.

## Budgets

One table (`search_budget`, keyed by `bucket` since 0019), one atomic statement, spent on
its own committed session before the guarded step. A refused attempt still counts.

| Bucket | Guards | Default | Environment |
| --- | --- | --- | --- |
| `search` | `/v1/search`, `/v1/ask`, `POST /v1/kt/{code}/ask` | 60 / 60 s | `SEARCH_RATE_LIMIT`, `SEARCH_RATE_WINDOW_S` |
| `kt_claim` | `POST /v1/kt/claim` | 10 / 60 s | `KT_CLAIM_RATE_LIMIT`, `KT_CLAIM_RATE_WINDOW_S` |
| `kt_summary` | `GET /v1/kt/{code}/handover-summary` | 6 / 60 s | `KT_SUMMARY_RATE_LIMIT`, `KT_SUMMARY_RATE_WINDOW_S` |

A 429 carries `Retry-After`. `0` is refused, never read as unlimited. Production runs on the
defaults; `docs/deploy.md` §6 says how to tune one.

## The trail

| Action | When | Meta |
| --- | --- | --- |
| `kt.created` / `kt.revoked` / `kt.completed` | lifecycle | — |
| `kt.extended` | `PATCH` with `extend_days` | `expires_at.from/to` |
| `kt.readdressed` | `PATCH` with `recipient_email` on an unclaimed package | — (never the address) |
| `kt.claimed` | first open binds the recipient | — |
| `kt.opened` | every later open | — |
| `kt.open` (`denied`) | a refused open, on its own transaction | `reason`, one of `kt.py`'s `DENIED_*` values: `unknown_code`, `bound_to_another_user`, `addressed_to_another_email`, `subject_of_package`, `revoked`, `completed`, `expired`, `unclaimed_via_read_route`, `claim_race_lost` — never the code |
| `kt.copilot_asked` | every copilot turn | `attempts`, `insufficient_evidence`, `citations`, `sources`, `query_tokens` — never the question |
| `kt.bookmarked` | a save | `kind` |
| `kt.conversation_archived` | an archive | `conversation_id` |

Every API-written KT row carries `correlation_id` = the response's `x-request-id`, and every
log line the request emitted carries `request_id`, `org_id` and an opaque `user_id`. An
administrator reads one package's history with `GET /v1/audit?resource_type=kt_package&
resource_id={id}`.

## Administration

`kt:manage` (owner, super_admin, hr_admin): create, list, detail, revoke, complete, and
`PATCH /v1/kt/{id}` to extend the expiry (from the later of now and the current expiry,
never past a year from today — a lapsed package can be reopened) or re-address an unclaimed
package (a claimed one refuses with 409). The admin page shows last activity and, per
package, a details panel with the record, the extend / re-address controls (offered only
where the server would accept them) and the package's activity from the trail.

The recipient is picked from the organisation's own employees, never typed. The list
leaves out the employee the package is about, who can never claim it, and accounts that
cannot sign in. A colleague missing from it has to be invited first, because a package
opens only inside the organisation that issued it. The API refuses the subject as
recipient on creation and on re-addressing alike (422). On the recipient's side,
`/handover` names the organisation the session is in (`GET /v1/me/organisation`), because
sign-in opens a person's oldest membership.

## What is honestly absent

* **Knowledge for a tenant that has synced nothing.** The worker now runs in production
  (ADR 0017: Cloud Tasks rings a private Cloud Run service), so a connected provider's
  documents are chunked, embedded and extracted within the drain that follows the sync.
  A tenant with no connection still sees the honest empty states and their reasons.
* **A graph leg.** See above.
* **Streaming.** The citation gate needs the whole answer and the Next proxy buffers bodies;
  streaming post-gate events is a separate decision.
* **Ranking people.** Non-negotiables 16–18 need signals exposed to the ranked person and a
  consent state; no consent field exists. Adding one is a migration and an ADR.

## Extending the console

Add the service function to `jutsu_api.kt_workspace`, call `_open_for` first, put every
narrowing beside `ACL_PREDICATE` inside the SQL (never after it), add the route to
`routers/kt_console.py` under `kt:open`, regenerate the client with `make api-types`, add the
`api.ktX` wrapper, and write the test against real Postgres in
`apps/api/tests/test_kt_workspace.py`. A new table needs `org_id`, FORCE RLS, composite
foreign keys to `(kt_packages.id, org_id)` and `(users.id, org_id)`, an entry in
`RLS_TABLES`, and one seeded row per tenant in `packages/db/tests/conftest.py`.
