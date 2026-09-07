"use client";

import { Fragment, useCallback, useId, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { CircleSlash, FileText, Loader2, Sparkles } from "lucide-react";

import { EvidenceSearch } from "@/components/product/evidence-search";
import { FailureState, LoadingRegion, Skeleton } from "@/components/states";
import { api, type AskCitation, type AskResponse, type Evidence } from "@/lib/api";
import { classifyApiError, isRetryable, type Failure } from "@/lib/api-error";

/**
 * Ask JUTSU — a grounded answer, its citations, and the evidence it stood on.
 *
 * Everything rendered here came from the backend: the answer text, the citation set
 * (validated server-side against the retrieved passages — the frontend never mints a
 * citation, §27), and the sources list. When the deployment has no answer provider the
 * API says so with a 503, and this component degrades to exactly what still works —
 * retrieval — with a sentence explaining which half is missing (§36).
 *
 * The citations are **operable**: §3 surface 1 and §24 both promise an answer whose
 * citations reach the source span, and a `[2]` that cannot be pressed is a promise the
 * page only appears to keep. Each marker in the prose, and each row of the sources list,
 * opens one panel that fetches `/v1/evidence/{chunk_id}` on demand — never eagerly, so an
 * answer citing eight documents costs one request for the one a reader actually opens.
 *
 * Conversation history is component state: the API is stateless and each question is
 * answered from evidence alone, so "history" here is a reading log, not context the
 * model sees. Making that true server-side is a product decision for later; pretending
 * it is already true would be worse.
 */

interface Exchange {
  question: string;
  response: AskResponse;
}

const SUGGESTED = [
  "What were the main responsibilities?",
  "Which decisions were important?",
  "What work is still unfinished?",
  "Who were the key collaborators?",
] as const;

/** One citation's source span, from the moment a reader asks for it. */
type SourceState =
  | { status: "loading" }
  | { status: "ready"; evidence: Evidence }
  | { status: "failed"; failure: Failure };

/**
 * What to say when a span cannot be fetched.
 *
 * `/v1/evidence/{chunk_id}` answers **404, not 403**, for a chunk the caller may not read
 * — deliberately, so the endpoint cannot be walked as an oracle. That makes a miss here
 * unreadable-or-gone rather than broken, and the shared `FailureState` would announce
 * "That did not load" in a large card: louder than the fact, and wrong about it.
 */
function sourceMessage(failure: Failure): string {
  if (failure.kind === "missing") {
    return "That source is not available to you any more. The document may have been superseded, or your access to it may have changed since this answer was written.";
  }
  return failure.message;
}

/** What a screen reader is told once an answer has landed. */
function announcement(response: AskResponse): string {
  if (response.insufficient_evidence) {
    return "No answer: the evidence you are authorised to read does not answer that question.";
  }
  const count = response.citations.length;
  if (count === 0) return "Answer ready. It cites no sources.";
  return `Answer ready, citing ${count} ${count === 1 ? "source" : "sources"}. Each citation marker opens its source span.`;
}

/** `[3]` in the answer prose. The capture group keeps the markers when splitting. */
const MARKER = /(\[\d+\])/g;
const IS_MARKER = /^\[(\d+)\]$/;

/**
 * The answer, with every marker the server cited turned into a control.
 *
 * A marker the citation set does not resolve stays prose. The set was validated against
 * the retrieved passages server-side (§27); a button for a number this page cannot
 * resolve would be the frontend inventing the one thing it must never invent.
 */
function AnswerProse({
  answer,
  citations,
  openMarker,
  panelId,
  onOpen,
}: {
  answer: string;
  citations: AskCitation[];
  openMarker: number | null;
  panelId: string;
  onOpen: (citation: AskCitation) => void;
}) {
  const byMarker = new Map(citations.map((citation) => [citation.marker, citation]));

  return (
    <p className="max-w-prose whitespace-pre-wrap text-pretty text-sm leading-relaxed text-foreground">
      {answer.split(MARKER).map((part, index) => {
        const match = IS_MARKER.exec(part);
        const citation = match ? byMarker.get(Number(match[1])) : undefined;
        if (!citation) return <Fragment key={index}>{part}</Fragment>;

        return (
          <button
            key={index}
            type="button"
            onClick={() => onOpen(citation)}
            aria-expanded={openMarker === citation.marker}
            aria-controls={panelId}
            aria-label={`Show source ${citation.marker}: ${citation.document_title}`}
            className={`mx-0.5 rounded font-mono text-brand underline decoration-dotted underline-offset-4 transition-colors hover:text-brand/80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand ${
              openMarker === citation.marker ? "bg-brand/10" : ""
            }`}
          >
            [{citation.marker}]
          </button>
        );
      })}
    </p>
  );
}

/** The row's own account of what pressing it did. "Shown" would lie about a failure. */
function rowLabel(state: SourceState | undefined, expanded: boolean): string {
  if (state?.status === "loading") return "Loading source";
  if (!expanded) return "View source";
  return state?.status === "failed" ? "Source unavailable" : "Source shown";
}

/**
 * One row of the resolved sources list, and the second way into the same panel.
 *
 * Somebody scanning an answer reaches for the list; somebody reading it reaches for the
 * marker. Both are the citation, so both open it.
 */
function SourceRow({
  citation,
  state,
  expanded,
  panelId,
  onOpen,
}: {
  citation: AskCitation;
  state: SourceState | undefined;
  expanded: boolean;
  panelId: string;
  onOpen: (citation: AskCitation) => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(citation)}
        aria-expanded={expanded}
        aria-controls={panelId}
        className="flex w-full flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg py-0.5 text-left text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
      >
        <span className="min-w-0 break-words">
          [{citation.marker}] {citation.document_title}
        </span>
        <span className="font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground/80">
          {citation.source_system}
        </span>
        <span className="inline-flex shrink-0 items-center gap-1.5 font-mono text-[0.625rem] uppercase tracking-[0.16em] text-brand">
          {state?.status === "loading" ? (
            <Loader2 aria-hidden="true" className="size-3 animate-spin motion-reduce:animate-none" />
          ) : (
            <FileText aria-hidden="true" className="size-3" />
          )}
          {rowLabel(state, expanded)}
        </span>
      </button>
    </li>
  );
}

/**
 * The cited span itself.
 *
 * The evidence text is rendered **whole** and never sliced with `char_start`/`char_end`.
 * Those offsets index the original document while `text` is the masked body, and masking
 * changes lengths — applying them here would highlight the wrong span quietly and
 * convincingly. The numbers are shown as numbers instead, which is the honest thing they
 * can do on this side of the masking.
 */
function SourcePanel({
  citation,
  state,
  onHide,
  onRetry,
}: {
  citation: AskCitation;
  state: SourceState | undefined;
  onHide: () => void;
  onRetry: () => void;
}) {
  return (
    <div className="rounded-xl border border-hairline bg-background/60 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
        <p className="eyebrow text-muted-foreground/80">
          Source [{citation.marker}] · {citation.document_title}
        </p>
        <button
          type="button"
          onClick={onHide}
          className="font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        >
          Hide source
        </button>
      </div>

      {state?.status === "loading" ? (
        <LoadingRegion label={`Loading the source span for citation ${citation.marker}.`}>
          <Skeleton className="mt-3 h-20 w-full" />
        </LoadingRegion>
      ) : null}

      {state?.status === "failed" ? (
        <>
          <p role="alert" className="mt-3 text-pretty text-sm leading-relaxed text-muted-foreground">
            {sourceMessage(state.failure)}
          </p>
          {isRetryable(state.failure) ? (
            <button
              type="button"
              onClick={onRetry}
              className="mt-3 rounded-lg border border-hairline-strong px-3 py-1.5 text-xs font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
            >
              Try again
            </button>
          ) : null}
        </>
      ) : null}

      {state?.status === "ready" ? (
        <>
          <p className="mt-3 whitespace-pre-wrap text-pretty text-sm leading-relaxed text-foreground">
            {state.evidence.text}
          </p>
          <p className="mt-3 font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground/80">
            {state.evidence.source_system} · chars {state.evidence.char_start}–{state.evidence.char_end}
          </p>
        </>
      ) : null}
    </div>
  );
}

function AnswerCard({ exchange }: { exchange: Exchange }) {
  const { question, response } = exchange;
  const panelId = useId();
  const [openMarker, setOpenMarker] = useState<number | null>(null);
  const [sources, setSources] = useState<Record<string, SourceState>>({});
  // One request per chunk, ever. A ref and not the state above: two presses inside one
  // tick both read the same render's `sources` and would both find it empty.
  const requested = useRef<Set<string>>(new Set());

  const load = useCallback(async (chunkId: string) => {
    requested.current.add(chunkId);
    setSources((current) => ({ ...current, [chunkId]: { status: "loading" } }));
    try {
      const evidence = await api.evidence(chunkId);
      setSources((current) => ({ ...current, [chunkId]: { status: "ready", evidence } }));
    } catch (error) {
      setSources((current) => ({
        ...current,
        [chunkId]: { status: "failed", failure: classifyApiError(error) },
      }));
    }
  }, []);

  const open = useCallback(
    (citation: AskCitation) => {
      // Opening is idempotent. Two controls address one panel, so a marker that toggled
      // would silently close what the row had just opened; dismissal lives on the panel,
      // where a reader can see it.
      setOpenMarker(citation.marker);
      if (!requested.current.has(citation.chunk_id)) void load(citation.chunk_id);
    },
    [load],
  );

  const openCitation =
    response.citations.find((citation) => citation.marker === openMarker) ?? null;

  return (
    <article className="flex flex-col gap-4 rounded-2xl border border-hairline bg-surface/40 p-6">
      <p className="text-sm font-medium text-foreground">{question}</p>

      {response.insufficient_evidence ? (
        <div className="flex items-start gap-3">
          <span
            aria-hidden="true"
            className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-hairline-strong bg-surface text-muted-foreground"
          >
            <CircleSlash className="size-4" />
          </span>
          <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
            The evidence you are authorised to read does not answer this. JUTSU refuses
            rather than guesses — try rephrasing, or search the sources directly below.
          </p>
        </div>
      ) : (
        <>
          <AnswerProse
            answer={response.answer ?? ""}
            citations={response.citations}
            openMarker={openMarker}
            panelId={panelId}
            onOpen={open}
          />
          {response.citations.length > 0 ? (
            <div>
              <h3 className="font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground">
                · Sources
              </h3>
              <ul className="mt-2 flex flex-col gap-1">
                {response.citations.map((citation) => (
                  <SourceRow
                    key={citation.marker}
                    citation={citation}
                    state={sources[citation.chunk_id]}
                    expanded={openMarker === citation.marker}
                    panelId={panelId}
                    onOpen={open}
                  />
                ))}
              </ul>
            </div>
          ) : null}

          {/* Always in the tree, so every `aria-controls` above points at something real. */}
          <div id={panelId}>
            {openCitation ? (
              <SourcePanel
                citation={openCitation}
                state={sources[openCitation.chunk_id]}
                onHide={() => setOpenMarker(null)}
                onRetry={() => void load(openCitation.chunk_id)}
              />
            ) : null}
          </div>
        </>
      )}
    </article>
  );
}

export function AskExperience() {
  const [draft, setDraft] = useState("");
  const [thread, setThread] = useState<Exchange[]>([]);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [notConfigured, setNotConfigured] = useState(false);

  const ask = useMutation({
    mutationFn: (question: string) => api.ask({ question }),
    onSuccess: (response, question) => {
      setFailure(null);
      setThread((current) => [{ question, response }, ...current]);
      setDraft("");
    },
    onError: (error: unknown) => {
      const classified = classifyApiError(error);
      if (classified.status === 503 && /not configured/i.test(classified.message)) {
        // The deployment cannot answer; retrieval still can. Degrade to it, once,
        // with the reason on screen — never a dead Ask box.
        setNotConfigured(true);
      } else {
        setFailure(classified);
      }
    },
  });

  function submit(question: string) {
    const trimmed = question.trim();
    if (trimmed && !ask.isPending) ask.mutate(trimmed);
  }

  if (notConfigured) {
    return (
      <div className="mt-10 flex flex-col gap-6">
        <div className="rounded-2xl border border-hairline bg-surface/40 p-5">
          <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
            Answer synthesis is not configured for this deployment yet, so questions
            cannot be answered in prose. Evidence retrieval works fully — search below,
            and every passage returned is real material you are authorised to read.
          </p>
        </div>
        <EvidenceSearch />
      </div>
    );
  }

  const latest = thread[0];

  return (
    <div className="mt-10 flex flex-col gap-6">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit(draft);
        }}
        className="flex flex-col gap-3 sm:flex-row"
      >
        <label htmlFor="ask-question" className="sr-only">
          Your question
        </label>
        <input
          id="ask-question"
          type="text"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Ask anything about your organisation's memory…"
          maxLength={4000}
          className="h-12 flex-1 rounded-xl border border-hairline-strong bg-surface/40 px-4 text-sm text-foreground placeholder:text-muted-foreground/80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        />
        <button
          type="submit"
          disabled={ask.isPending || !draft.trim()}
          aria-busy={ask.isPending}
          className="inline-flex h-12 items-center justify-center gap-2 rounded-xl bg-brand px-6 text-[0.9375rem] font-semibold text-brand-foreground transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60 sm:w-36"
        >
          {ask.isPending ? (
            <Loader2 aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" />
          ) : (
            <Sparkles aria-hidden="true" className="size-4" />
          )}
          {ask.isPending ? "Thinking…" : "Ask"}
        </button>
      </form>

      {/*
        The end of the wait, spoken. `LoadingRegion` announces that an answer is being
        composed and then nothing announces that one arrived, so a screen-reader user
        hears "Composing an answer" followed by silence over a screenful of new text.
        Mounted from the first render rather than with the answer: a live region added to
        the page at the same moment as its content is frequently not announced at all.

        The question is included because a live region whose text does not CHANGE is
        not re-announced: two questions that both cite two sources would otherwise be
        one announcement, and the second answer would arrive in silence.
      */}
      <p role="status" aria-live="polite" className="sr-only">
        {latest ? `${latest.question} — ${announcement(latest.response)}` : ""}
      </p>

      {thread.length === 0 && !ask.isPending ? (
        <div className="flex flex-wrap gap-2" role="group" aria-label="Suggested questions">
          {SUGGESTED.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => submit(suggestion)}
              className="rounded-full border border-hairline-strong px-3.5 py-1.5 text-xs text-muted-foreground transition-colors hover:border-brand/40 hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
            >
              {suggestion}
            </button>
          ))}
        </div>
      ) : null}

      {ask.isPending ? (
        <LoadingRegion label="Composing an answer from retrieved evidence.">
          <div
            aria-hidden="true"
            className="h-24 animate-pulse rounded-2xl border border-hairline bg-surface/40 motion-reduce:animate-none"
          />
        </LoadingRegion>
      ) : null}

      {failure ? (
        <FailureState
          failure={failure}
          onRetry={thread.length === 0 && draft ? () => submit(draft) : undefined}
          deniedWhat="asking questions"
        />
      ) : null}

      {thread.map((exchange, index) => (
        <AnswerCard key={`${index}-${exchange.question}`} exchange={exchange} />
      ))}

      {thread.length > 0 ? (
        <p className="max-w-prose text-pretty text-xs leading-relaxed text-muted-foreground">
          Every answer above is assembled from evidence you are authorised to read, and
          every citation was validated against the retrieved passages before it reached
          this page. When the evidence cannot answer, JUTSU says so.
        </p>
      ) : null}
    </div>
  );
}
