"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { CircleSlash, Loader2, Sparkles } from "lucide-react";

import { EvidenceSearch } from "@/components/product/evidence-search";
import { FailureState, LoadingRegion } from "@/components/states";
import { api, type AskResponse } from "@/lib/api";
import { classifyApiError, type Failure } from "@/lib/api-error";

/**
 * Ask JUTSU — a grounded answer, its citations, and the evidence it stood on.
 *
 * Everything rendered here came from the backend: the answer text, the citation set
 * (validated server-side against the retrieved passages — the frontend never mints a
 * citation, §27), and the sources list. When the deployment has no answer provider the
 * API says so with a 503, and this component degrades to exactly what still works —
 * retrieval — with a sentence explaining which half is missing (§36).
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

function CitationChip({ marker, title }: { marker: number; title: string }) {
  return (
    <li className="flex items-baseline gap-2 text-xs text-muted-foreground">
      <span className="font-mono text-brand">[{marker}]</span>
      <span className="truncate">{title}</span>
    </li>
  );
}

function AnswerCard({ exchange }: { exchange: Exchange }) {
  const { question, response } = exchange;
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
          {/* The answer text carries its [n] markers inline; the list below is the same
              set resolved to documents. Both came from the server. */}
          <p className="max-w-prose whitespace-pre-wrap text-pretty text-sm leading-relaxed text-foreground">
            {response.answer}
          </p>
          {response.citations.length > 0 ? (
            <div>
              <h3 className="font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground">
                · Sources
              </h3>
              <ul className="mt-2 flex flex-col gap-1">
                {response.citations.map((citation) => (
                  <CitationChip
                    key={citation.marker}
                    marker={citation.marker}
                    title={`${citation.document_title} (${citation.source_system})`}
                  />
                ))}
              </ul>
            </div>
          ) : null}
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
