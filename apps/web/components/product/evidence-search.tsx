"use client";

import { useCallback, useState } from "react";
import { AlertTriangle, FileText, Loader2, Search } from "lucide-react";

import { ErrorState, LoadingRegion, Skeleton } from "@/components/states";
import { api, type Evidence, type SearchResult } from "@/lib/api";
import { classifyApiError, isRetryable, type Failure } from "@/lib/api-error";

/**
 * Evidence retrieval over `POST /v1/search`. **Not** the cited-answer surface.
 *
 * `/ask` promises a grounded answer whose every claim is clickable; that needs answer
 * synthesis, which is slice S18–S19 and does not exist. What exists is the retrieval
 * half, so this renders exactly that and says so. §4.11 forbids mock data behind a
 * surface — it does not forbid shipping the real half of one, provided the page is
 * honest about which half. `surfaces.ts` therefore still reads `stub`.
 *
 * **The request carries only `query`, `k` and `cursor`.** No organisation, no user, no
 * principals, no ACL filter, no score floor. The tenant comes from the session cookie
 * and the principals are resolved inside the SQL per request, so there is nothing here a
 * browser could edit to widen what it sees — and adding a field that looked like a
 * filter would be the regression ADR 0011 exists to prevent.
 */

/** How many chunks a page asks for. Within the API's 1..100 bound. */
const PAGE_SIZE = 10;

/** The API's own limit, mirrored so the field can refuse before a round trip. */
const MAX_QUERY_CHARS = 4000;

function Score({ value }: { value: number }) {
  // Two decimals: the difference between 0.5929 and 0.5884 is not a difference a reader
  // should be invited to act on, and rendering four implies a precision the ranking does
  // not carry.
  return (
    <span className="shrink-0 font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground">
      {value.toFixed(2)}
    </span>
  );
}

/**
 * One retrieved chunk, with its source span available on request.
 *
 * The masked text is rendered as-is and **never** sliced with `char_start`/`char_end`.
 * Those index the original document, and masking changes lengths, so applying them here
 * would highlight the wrong span — quietly, and convincingly. "View source span" fetches
 * the pair that actually belong together.
 */
function Result({ item }: { item: SearchResult }) {
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const open = useCallback(async () => {
    if (evidence || loading) return;
    setLoading(true);
    setFailure(null);
    try {
      setEvidence(await api.evidence(item.chunk_id));
    } catch (error) {
      setFailure(classifyApiError(error).message);
    } finally {
      setLoading(false);
    }
  }, [evidence, loading, item.chunk_id]);

  return (
    <li className="rounded-2xl border border-hairline bg-surface/40 p-5">
      <div className="flex items-start justify-between gap-4">
        <h3 className="text-pretty text-sm font-medium leading-relaxed">
          {item.document_title || <span className="text-muted-foreground">Untitled document</span>}
        </h3>
        <Score value={item.score} />
      </div>

      <p className="mt-3 whitespace-pre-wrap text-pretty text-sm leading-relaxed text-muted-foreground">
        {item.text}
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground/80">
        <span className="font-mono">{item.source_system}</span>
        <span className="font-mono">
          chars {item.char_start}–{item.char_end}
        </span>
        <button
          type="button"
          onClick={open}
          disabled={loading || evidence !== null}
          className="inline-flex items-center gap-1.5 rounded-md tracking-[0.14em] text-brand transition-colors hover:text-brand/80 disabled:text-muted-foreground/60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        >
          {loading ? (
            <Loader2 aria-hidden="true" className="h-3 w-3 animate-spin motion-reduce:animate-none" />
          ) : (
            <FileText aria-hidden="true" className="h-3 w-3" />
          )}
          {evidence ? "Source span shown" : "View source span"}
        </button>
      </div>

      {failure ? (
        <p role="alert" className="mt-3 text-xs text-muted-foreground">
          {failure}
        </p>
      ) : null}

      {evidence ? (
        <div className="mt-4 rounded-xl border border-hairline bg-background/60 p-4">
          <p className="eyebrow text-muted-foreground/80">Source span</p>
          <p className="mt-2 whitespace-pre-wrap text-pretty text-sm leading-relaxed">
            {evidence.text}
          </p>
          <p className="mt-3 font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground/80">
            {evidence.source_system} · chars {evidence.char_start}–{evidence.char_end}
          </p>
        </div>
      ) : null}
    </li>
  );
}

export function EvidenceSearch() {
  const [draft, setDraft] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [items, setItems] = useState<SearchResult[]>([]);
  const [stats, setStats] = useState<{ returned: number; elapsed_ms: number; exhausted: boolean } | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [more, setMore] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);

  const run = useCallback(async (query: string, after: string | null) => {
    const appending = after !== null;
    if (appending) setMore(true);
    else setBusy(true);
    setFailure(null);

    try {
      // The entire request. Three fields, and every one of them is about relevance.
      const page = await api.search({ query, k: PAGE_SIZE, cursor: after });
      setItems((previous) => (appending ? [...previous, ...page.items] : page.items));
      setStats(page.stats);
      setCursor(page.next_cursor ?? null);
      setSubmitted(query);
    } catch (error) {
      setFailure(classifyApiError(error));
      if (!appending) {
        setItems([]);
        setStats(null);
        setCursor(null);
      }
    } finally {
      setBusy(false);
      setMore(false);
    }
  }, []);

  const submit = useCallback(
    (event: React.FormEvent) => {
      event.preventDefault();
      const query = draft.trim();
      if (!query || busy) return;
      void run(query, null);
    },
    [busy, draft, run],
  );

  const searched = submitted !== null && !busy && failure === null;

  return (
    <div className="mt-10">
      <form onSubmit={submit} className="flex flex-col gap-3 sm:flex-row">
        <div className="relative flex-1">
          <Search
            aria-hidden="true"
            className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          />
          <input
            type="search"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            maxLength={MAX_QUERY_CHARS}
            placeholder="Search the corpus — a question, a project, a decision"
            aria-label="Search query"
            className="w-full rounded-xl border border-hairline bg-surface/40 py-3 pl-11 pr-4 text-sm placeholder:text-muted-foreground/70 focus-visible:border-brand/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          />
        </div>
        <button
          type="submit"
          disabled={busy || draft.trim().length === 0}
          className="inline-flex shrink-0 items-center justify-center gap-2 rounded-xl border border-hairline-strong px-5 py-3 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        >
          {busy ? (
            <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin motion-reduce:animate-none" />
          ) : null}
          Search
        </button>
      </form>

      {busy ? (
        <LoadingRegion label="Searching the corpus">
          <ul className="mt-8 space-y-4">
            {[0, 1, 2].map((n) => (
              <li key={n}>
                <Skeleton className="h-32 w-full" />
              </li>
            ))}
          </ul>
        </LoadingRegion>
      ) : null}

      {failure ? (
        <div className="mt-8">
          <ErrorState
            message={failure.message}
            requestId={failure.requestId}
            onRetry={
              // A 401 needs a sign-in, not a retry, and a 422 needs the query changed.
              // Offering "Try again" for either teaches people to click a button that
              // cannot work. `isRetryable` is shared so every surface agrees which is which.
              isRetryable(failure) ? () => void run(submitted ?? draft.trim(), null) : undefined
            }
          />
        </div>
      ) : null}

      {searched && items.length === 0 ? (
        <div className="mt-8 rounded-2xl border border-hairline bg-surface/40 p-6">
          <p className="eyebrow text-muted-foreground/80">No matching evidence</p>
          <p className="mt-3 text-pretty text-sm leading-relaxed text-muted-foreground">
            Nothing you are permitted to read matched that search. Results are filtered by
            the access you already hold on the underlying documents, so a colleague may
            see different results for the same words.
          </p>
        </div>
      ) : null}

      {items.length > 0 ? (
        <>
          {stats ? (
            <p className="mt-8 text-xs text-muted-foreground" data-testid="search-stats">
              {stats.returned} {stats.returned === 1 ? "result" : "results"} in {stats.elapsed_ms} ms
            </p>
          ) : null}

          <ul className="mt-4 space-y-4">
            {items.map((item) => (
              <Result key={item.chunk_id} item={item} />
            ))}
          </ul>

          {/*
            `exhausted` is information, never an error. It means the search stopped short
            of `k`, which usually means the caller is not authorised to see `k`
            documents — the system working. A UI that renders it as a warning teaches
            people the search is broken when it is being correct.
          */}
          {stats?.exhausted ? (
            <p className="mt-6 text-xs text-muted-foreground" data-testid="search-exhausted">
              That is everything matching within the documents you can read.
            </p>
          ) : null}

          {cursor ? (
            <button
              type="button"
              onClick={() => void run(submitted ?? "", cursor)}
              disabled={more}
              className="mt-6 inline-flex items-center gap-2 rounded-xl border border-hairline-strong px-5 py-2.5 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
            >
              {more ? (
                <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin motion-reduce:animate-none" />
              ) : null}
              Load more
            </button>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

/**
 * The banner that keeps this page honest.
 *
 * Separated so the distinction is a component rather than a paragraph somebody edits
 * away: what ships is retrieval, and cited answering is still ahead.
 */
export function RetrievalOnlyNotice({ slice }: { slice: string }) {
  return (
    <div className="mt-8 flex gap-3 rounded-2xl border border-hairline bg-surface/40 p-5">
      <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
      <div>
        <p className="eyebrow text-muted-foreground/80">Evidence retrieval, not answers yet</p>
        <p className="mt-2 text-pretty text-sm leading-relaxed text-muted-foreground">
          This searches the corpus and returns the real passages you are permitted to
          read. It does not yet compose a written answer with citations — that is slice{" "}
          <span className="font-mono text-foreground">{slice}</span>. Nothing below is
          generated or mocked; every passage is a document you have access to.
        </p>
      </div>
    </div>
  );
}
