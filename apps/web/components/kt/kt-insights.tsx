"use client";

import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { Pill, When } from "@/components/admin/page-scaffold";
import { EmptyState, LoadingRegion, Skeleton } from "@/components/states";
import { KtFailure } from "@/components/kt/kt-failure";
import { useKtPackage } from "@/components/kt/kt-shell";
import { api, type Evidence, type KtInsight, type KtProgressState } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * The knowledge tabs, backed by extraction_claims.
 *
 * Every row on these screens survived the extraction quote gate — its `quote` appears
 * verbatim in the evidence chunk it anchors to — and passed the recipient's own ACL
 * over that evidence at read time. Nothing here is generated for display: the quote IS
 * the citation, shown with the claim (§23's "source citation").
 *
 * The empty state distinguishes the two honest reasons for emptiness: extraction has
 * not run on this deployment, or it ran and nothing in scope is readable by this
 * recipient. The package cannot widen either.
 */

const TYPE_SCOPE: Record<string, string> = {
  decision: "decisions",
  person: "people",
  project: "projects",
  meeting: "meetings",
  responsibility: "responsibilities",
};

/** The recipient's progress marks, keyed the way the API keys them (`claim:{id}`). */
function useProgressMap(code: string, enabled = true) {
  const progress = useQuery({
    queryKey: ["kt", code, "progress"],
    queryFn: () => api.ktProgress(code),
    // An out-of-scope tab renders a notice and no cards; it has no marks to look up.
    enabled,
  });
  const byKey = new Map<string, string>();
  for (const item of progress.data?.items ?? []) byKey.set(item.item_key, item.state);
  return { progress, byKey };
}

function progressPill(state: string) {
  if (state === "done") return <Pill tone="good">done</Pill>;
  if (state === "unclear") return <Pill tone="attention">unclear</Pill>;
  return <Pill tone="neutral">{state}</Pill>;
}

const CARD_ACTION_CLASS =
  "inline-flex items-center gap-1.5 rounded-md font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-brand transition-colors hover:text-brand/80 disabled:text-muted-foreground/60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand";

/**
 * One claim, with what the recipient can do about it.
 *
 * `progressState` is the recipient's own mark on this claim, looked up by the list from
 * one `GET /progress` rather than fetched per card. The masked source text is fetched
 * on request and rendered as-is — **never** sliced with `char_start`/`char_end`. Those
 * index the original document, and masking changes lengths, so applying them here would
 * highlight the wrong span, quietly and convincingly. `/v1/evidence/{chunk_id}` returns
 * the pair that actually belong together.
 */
function InsightCard({
  insight,
  code,
  progressState,
}: {
  insight: KtInsight;
  code: string;
  progressState?: string;
}) {
  const queryClient = useQueryClient();
  const headline =
    insight.name && insight.summary
      ? `${insight.name} — ${insight.summary}`
      : (insight.name ?? insight.summary ?? insight.quote);
  const itemKey = `claim:${insight.id}`;

  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const viewSource = useCallback(async () => {
    if (evidence || loading) return;
    setLoading(true);
    setFailure(null);
    try {
      setEvidence(await api.evidence(insight.chunk_id));
    } catch (error) {
      setFailure(classifyApiError(error).message);
    } finally {
      setLoading(false);
    }
  }, [evidence, loading, insight.chunk_id]);

  const save = useMutation({
    mutationFn: () => api.ktBookmark(code, { kind: "claim", ref_id: insight.id }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["kt", code, "bookmarks"] });
      toast.success("Saved to your items.");
    },
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  const invalidateProgress = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["kt", code, "progress"] }),
      queryClient.invalidateQueries({ queryKey: ["kt", code, "workspace"] }),
    ]);

  const setProgress = useMutation({
    mutationFn: (state: KtProgressState) => api.ktSetProgress(code, itemKey, state),
    onSuccess: invalidateProgress,
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  const clearProgress = useMutation({
    mutationFn: () => api.ktClearProgress(code, itemKey),
    onSuccess: invalidateProgress,
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  const busy = save.isPending || setProgress.isPending || clearProgress.isPending;

  return (
    <li className="flex flex-col gap-2 rounded-xl border border-hairline bg-surface/40 p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="min-w-0 text-sm font-medium text-foreground">{headline}</p>
        <span className="shrink-0 font-mono text-[0.625rem] uppercase tracking-[0.14em] text-muted-foreground">
          {insight.date ?? <When iso={insight.occurred_at} />}
        </span>
      </div>
      {/* The verbatim evidence, exactly as the quote gate verified it. */}
      <blockquote className="border-l-2 border-brand/40 pl-3 text-pretty text-xs leading-relaxed text-muted-foreground">
        &ldquo;{insight.quote}&rdquo;
      </blockquote>
      <p className="font-mono text-[0.625rem] uppercase tracking-[0.14em] text-muted-foreground">
        {insight.document_title} · confidence {insight.confidence.toFixed(2)}
      </p>

      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-2">
        <button
          type="button"
          onClick={() => void viewSource()}
          disabled={loading || evidence !== null}
          aria-label={`View source for: ${headline}`}
          className={CARD_ACTION_CLASS}
        >
          {loading ? (
            <Loader2 aria-hidden="true" className="h-3 w-3 animate-spin motion-reduce:animate-none" />
          ) : (
            <FileText aria-hidden="true" className="h-3 w-3" />
          )}
          {evidence ? "Source shown" : "View source"}
        </button>
        <button
          type="button"
          onClick={() => save.mutate()}
          disabled={busy}
          aria-label={`Save to your items: ${headline}`}
          className={CARD_ACTION_CLASS}
        >
          {save.isPending ? "Saving…" : "Save"}
        </button>
        {progressState ? progressPill(progressState) : null}
        {progressState !== "done" ? (
          <button
            type="button"
            onClick={() => setProgress.mutate("done")}
            disabled={busy}
            aria-label={`Mark done: ${headline}`}
            className={CARD_ACTION_CLASS}
          >
            Mark done
          </button>
        ) : null}
        {progressState !== "unclear" ? (
          <button
            type="button"
            onClick={() => setProgress.mutate("unclear")}
            disabled={busy}
            aria-label={`Still unclear: ${headline}`}
            className={CARD_ACTION_CLASS}
          >
            Still unclear
          </button>
        ) : null}
        {progressState ? (
          <button
            type="button"
            onClick={() => clearProgress.mutate()}
            disabled={busy}
            aria-label={`Clear progress mark: ${headline}`}
            className={CARD_ACTION_CLASS}
          >
            Clear
          </button>
        ) : null}
      </div>

      {failure ? (
        <p role="alert" className="text-xs text-muted-foreground">
          {failure}
        </p>
      ) : null}

      {evidence ? (
        <div className="mt-2 rounded-xl border border-hairline bg-background/60 p-4">
          <p className="eyebrow text-muted-foreground/80">Source · {evidence.document_title}</p>
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

export function KtInsightsList({
  claimType,
  title,
  emptyWord,
}: {
  claimType: string | null;
  title: string;
  emptyWord: string;
}) {
  const { pkg, code } = useKtPackage();

  const category = claimType ? TYPE_SCOPE[claimType] : null;
  const inScope = claimType === null || (category !== null && pkg.scope.includes(category));

  const insights = useQuery({
    queryKey: ["kt", code, "insights", claimType],
    queryFn: () => api.ktInsights(code, { type: claimType }),
    enabled: inScope,
  });
  // One request for the whole list; each card is handed its own mark. A failure here
  // leaves the cards unmarked and says so — it never hides the claims themselves.
  const { progress, byKey } = useProgressMap(code, inScope);

  if (!inScope) {
    return (
      <div className="flex flex-col gap-6">
        <h2 className="display text-xl font-semibold">{title}</h2>
        <EmptyState title={`${title} are not part of this package`}>
          <p>The administrator scoped this package to: {pkg.scope.join(", ")}.</p>
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <h2 className="display text-xl font-semibold">{title}</h2>
      {insights.error ? (
        <KtFailure
          failure={classifyApiError(insights.error)}
          onRetry={() => void insights.refetch()}
        />
      ) : insights.isPending ? (
        <LoadingRegion label={`Loading ${emptyWord}.`}>
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-20" />
            ))}
          </div>
        </LoadingRegion>
      ) : insights.data.items.length === 0 ? (
        <EmptyState title={`No ${emptyWord} you are authorised to read`}>
          <p>
            {emptyWord.charAt(0).toUpperCase() + emptyWord.slice(1)} come from knowledge
            extraction over the documents your account may read. Nothing extracted in
            this package&apos;s window is visible to you yet — either extraction has not
            run on those documents, or their access lists do not include you. Nothing on
            this screen is ever invented to fill the gap.
          </p>
        </EmptyState>
      ) : (
        <>
          {progress.error ? (
            <p role="status" className="text-xs text-muted-foreground">
              Your progress marks did not load: {classifyApiError(progress.error).message}
            </p>
          ) : null}
          <ul className="flex flex-col gap-3">
            {insights.data.items.map((insight) => (
              <InsightCard
                key={insight.id}
                insight={insight}
                code={code}
                progressState={byKey.get(`claim:${insight.id}`)}
              />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/** The chronological view: every in-scope claim type, date-ordered by the backend. */
export function KtTimeline() {
  const { code } = useKtPackage();

  const insights = useQuery({
    queryKey: ["kt", code, "insights", null],
    queryFn: () => api.ktInsights(code, {}),
  });

  return (
    <div className="flex flex-col gap-6">
      <h2 className="display text-xl font-semibold">Timeline</h2>
      {insights.error ? (
        <KtFailure
          failure={classifyApiError(insights.error)}
          onRetry={() => void insights.refetch()}
        />
      ) : insights.isPending ? (
        <LoadingRegion label="Loading the timeline.">
          <Skeleton className="h-64" />
        </LoadingRegion>
      ) : insights.data.items.length === 0 ? (
        <EmptyState title="Nothing on the timeline yet">
          <p>
            The timeline is built from extracted decisions, meetings and project events
            you are authorised to read. It fills as extraction runs over the
            package&apos;s documents.
          </p>
        </EmptyState>
      ) : (
        <ol className="relative flex flex-col gap-5 border-l border-hairline pl-6">
          {insights.data.items.map((insight) => (
            <li key={insight.id} className="relative">
              <span
                aria-hidden="true"
                className="absolute -left-[1.85rem] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-brand bg-background"
              />
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-brand">
                  {insight.date ?? <When iso={insight.occurred_at} />}
                </span>
                <Pill tone="neutral">{insight.claim_type}</Pill>
              </div>
              <p className="mt-1 text-sm text-foreground">
                {insight.summary ?? insight.name ?? insight.quote}
              </p>
              <p className="mt-0.5 font-mono text-[0.625rem] uppercase tracking-[0.14em] text-muted-foreground">
                {insight.document_title}
              </p>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
