"use client";

import { useQuery } from "@tanstack/react-query";

import { Pill, When } from "@/components/admin/page-scaffold";
import { EmptyState, FailureState, LoadingRegion, Skeleton } from "@/components/states";
import { useKtPackage } from "@/components/kt/kt-shell";
import { api, type KtInsight } from "@/lib/api";
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

function InsightCard({ insight }: { insight: KtInsight }) {
  const headline =
    insight.name && insight.summary
      ? `${insight.name} — ${insight.summary}`
      : (insight.name ?? insight.summary ?? insight.quote);
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
        <FailureState
          failure={classifyApiError(insights.error)}
          onRetry={() => void insights.refetch()}
          deniedWhat={`reading this package's ${emptyWord}`}
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
        <ul className="flex flex-col gap-3">
          {insights.data.items.map((insight) => (
            <InsightCard key={insight.id} insight={insight} />
          ))}
        </ul>
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
        <FailureState
          failure={classifyApiError(insights.error)}
          onRetry={() => void insights.refetch()}
          deniedWhat="reading this package's timeline"
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
