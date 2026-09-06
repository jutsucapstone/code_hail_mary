"use client";

import Link from "next/link";
import { createContext, useContext } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Pill } from "@/components/admin/page-scaffold";
import { EmptyState, LoadingRegion, Skeleton } from "@/components/states";
import { KtFailure } from "@/components/kt/kt-failure";
import { useKtPackage } from "@/components/kt/kt-shell";
import {
  api,
  type KtGap,
  type KtLearningItem,
  type KtLearningStage,
  type KtProgressState,
  type KtRecommendation,
  type KtWorkspace,
} from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import { cn } from "@/lib/utils";

/**
 * The personalised workspace: where a recipient is, what to read next, what is still
 * unclear, and how much of the package's text the evidence actually covers.
 *
 * One request serves all of it — `GET /v1/kt/{code}/workspace` — computed at read time
 * from what THIS recipient may read and what they have marked. Nothing on these panels
 * is a stored score: the coverage figure is the extraction run over documents the
 * caller's own ACL admits, and it is rendered only when the backend says it is reliable
 * (§36). When it is not, the backend's reason is what shows, and no percentage does.
 *
 * One request, one observer: `WorkspaceRegion` owns the query and its loading and failure
 * states, and hands the payload to the panels through context. The panels never fetch.
 */

const WORKSPACE_KEY = (code: string) => ["kt", code, "workspace"] as const;

/**
 * The workspace payload, provided by `WorkspaceRegion`.
 *
 * When every panel mounted its own `useQuery` over the shared key, each panel appearing
 * after the first resolve was a new observer over stale data and refetched — a duplicate
 * GET on every overview load, and a re-render that could unmount a button between
 * pointer-down and click. A longer `staleTime` would have hidden that, and would also have
 * kept evidence-derived labels on screen for that long after a revocation; `api.ts` asks
 * for a short one on every KT route for exactly that reason. One observer needs neither.
 */
const WorkspaceContext = createContext<KtWorkspace | null>(null);

function useWorkspace(): KtWorkspace {
  const value = useContext(WorkspaceContext);
  if (value === null) {
    throw new Error("Workspace panels render inside WorkspaceRegion");
  }
  return value;
}

/** `/kt/{code}` for the overview (tab `""`), `/kt/{code}/{tab}` otherwise. */
function hrefFor(code: string, tab: string): string {
  const base = `/kt/${encodeURIComponent(code)}`;
  return tab ? `${base}/${tab}` : base;
}

const LINK_CLASS =
  "rounded-sm text-sm font-medium text-foreground underline-offset-4 transition-colors hover:text-brand hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand";

const BUTTON_CLASS =
  "rounded-lg border border-hairline-strong px-3 py-1.5 text-xs font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60";

function PanelHeading({ id, children }: { id: string; children: React.ReactNode }) {
  return (
    <h2 id={id} className="display text-xl font-semibold">
      {children}
    </h2>
  );
}

/**
 * The one place the workspace query's three states render.
 *
 * The overview mounts several panels over the same payload; giving each its own skeleton
 * and its own failure notice would show five of everything. The children render only
 * once data exists, and read it from context rather than fetching.
 */
export function WorkspaceRegion({
  children,
  skeleton,
}: {
  children: React.ReactNode;
  skeleton?: React.ReactNode;
}) {
  const { code } = useKtPackage();
  const workspace = useQuery({
    queryKey: WORKSPACE_KEY(code),
    queryFn: () => api.ktWorkspace(code),
  });

  if (workspace.error) {
    return (
      <KtFailure
        failure={classifyApiError(workspace.error)}
        onRetry={() => void workspace.refetch()}
      />
    );
  }
  if (workspace.isPending) {
    return (
      <LoadingRegion label="Loading your workspace.">
        {skeleton ?? (
          <div className="flex flex-col gap-6">
            <Skeleton className="h-28" />
            <div className="grid gap-6 lg:grid-cols-2">
              <Skeleton className="h-40" />
              <Skeleton className="h-40" />
            </div>
            <Skeleton className="h-24" />
            <Skeleton className="h-32" />
          </div>
        )}
      </LoadingRegion>
    );
  }
  return <WorkspaceContext.Provider value={workspace.data}>{children}</WorkspaceContext.Provider>;
}

/**
 * Progress writes, shared by the learning path and the gaps panel.
 *
 * Every write invalidates the workspace query: the path's pills, the resume card's
 * counts, the recommendations and the gaps are all derived from progress on the server,
 * and a local patch of one of them would leave the others describing the old state.
 */
function useProgressWrites() {
  const { code } = useKtPackage();
  const client = useQueryClient();
  const invalidate = () => client.invalidateQueries({ queryKey: WORKSPACE_KEY(code) });
  const onError = (error: unknown) => {
    toast.error(classifyApiError(error).message);
  };

  const set = useMutation({
    mutationFn: ({ key, state }: { key: string; state: KtProgressState }) =>
      api.ktSetProgress(code, key, state),
    onSuccess: invalidate,
    onError,
  });
  const clear = useMutation({
    mutationFn: ({ key }: { key: string }) => api.ktClearProgress(code, key),
    onSuccess: invalidate,
    onError,
  });
  return { set, clear };
}

// ------------------------------------------------------------------ resume card

export function ResumeCard() {
  const { pkg, code } = useKtPackage();
  const { resume } = useWorkspace();

  const returning = resume.last_activity_at !== null || resume.last_conversation !== null;

  if (!returning) {
    const subject = pkg.subject;
    const role = [subject.role_title, subject.role_level].filter(Boolean).join(" · ");
    return (
      <section
        aria-labelledby="kt-resume-heading"
        className="flex flex-col gap-2 rounded-2xl border border-hairline bg-surface/40 p-6"
      >
        <PanelHeading id="kt-resume-heading">Your knowledge transfer</PanelHeading>
        <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          You&apos;re taking over from{" "}
          <span className="text-foreground">{subject.display_name ?? "a colleague"}</span>
          {role ? <> · {role}</> : null}
        </p>
      </section>
    );
  }

  const facts: string[] = [];
  if (resume.path_total > 0) {
    facts.push(`${resume.path_done} of ${resume.path_total} learning steps done`);
  }
  if (resume.unclear > 0) facts.push(`${resume.unclear} still unclear`);
  if (resume.bookmarks > 0) facts.push(`${resume.bookmarks} saved`);

  return (
    <section
      aria-labelledby="kt-resume-heading"
      className="flex flex-col gap-3 rounded-2xl border border-hairline bg-surface/40 p-6"
    >
      <PanelHeading id="kt-resume-heading">Welcome back.</PanelHeading>
      <p className="text-sm text-muted-foreground">Continue where you left off.</p>
      {resume.last_conversation ? (
        <p className="text-sm">
          <Link
            href={`${hrefFor(code, "ask")}?conversation=${encodeURIComponent(resume.last_conversation.id)}`}
            className={LINK_CLASS}
          >
            {resume.last_conversation.title ?? "Your last conversation"}
          </Link>
        </p>
      ) : null}
      {facts.length > 0 ? (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
          {facts.map((fact) => (
            <li key={fact} className="tabular-nums">
              {fact}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

// --------------------------------------------------------------- coverage panel

export function CoveragePanel() {
  const { coverage } = useWorkspace();

  // The percentage renders only on a reliable figure with a ratio to show. `reliable`
  // alone is the backend's word; the null check is the type's. Neither is inferred here.
  const percentage =
    coverage.reliable && coverage.extraction_ratio !== null
      ? Math.round(coverage.extraction_ratio * 100)
      : null;

  return (
    <section
      aria-labelledby="kt-coverage-heading"
      className="flex flex-col gap-4 rounded-2xl border border-hairline bg-surface/40 p-6"
    >
      <PanelHeading id="kt-coverage-heading">Knowledge coverage</PanelHeading>
      {coverage.categories.length > 0 ? (
        <ul className="flex flex-col gap-1.5 text-sm text-foreground">
          {coverage.categories.map((bucket) => (
            <li key={bucket.category} className="tabular-nums">
              {bucket.claims_visible} {bucket.category} you can read
            </li>
          ))}
        </ul>
      ) : null}
      <p className="text-sm text-foreground tabular-nums">
        {coverage.documents_visible} documents in the window you can read,{" "}
        {coverage.documents_extracted} of them extracted
      </p>
      {percentage !== null ? (
        <>
          <p className="text-sm text-foreground tabular-nums">
            Extraction has covered {percentage}% of the text you can read.
          </p>
          <p className="max-w-prose text-pretty text-xs leading-relaxed text-muted-foreground">
            {coverage.reason}
          </p>
        </>
      ) : (
        <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          {coverage.reason}
        </p>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- recommended

function RecommendationRow({ item, code }: { item: KtRecommendation; code: string }) {
  return (
    <li className="flex flex-col gap-1 rounded-xl border border-hairline bg-background px-5 py-3.5">
      <Link href={hrefFor(code, item.tab)} className={cn(LINK_CLASS, "self-start")}>
        {item.label}
      </Link>
      <p className="text-pretty text-xs leading-relaxed text-muted-foreground">{item.why}</p>
    </li>
  );
}

export function Recommended() {
  const { code } = useKtPackage();
  const { recommendations } = useWorkspace();

  return (
    <section aria-labelledby="kt-recommended-heading" className="flex flex-col gap-4">
      <PanelHeading id="kt-recommended-heading">Recommended next</PanelHeading>
      {recommendations.length === 0 ? (
        <EmptyState title="Nothing to recommend yet">
          <p>
            Recommendations come from the evidence you can read and your own progress; as
            either grows, this fills.
          </p>
        </EmptyState>
      ) : (
        <ul className="flex flex-col gap-2">
          {recommendations.map((item) => (
            <RecommendationRow key={item.key} item={item} code={code} />
          ))}
        </ul>
      )}
    </section>
  );
}

// -------------------------------------------------------------- still unclear

function GapRow({
  gap,
  code,
  onUnderstood,
  pending,
}: {
  gap: KtGap;
  code: string;
  onUnderstood?: () => void;
  pending: boolean;
}) {
  return (
    <li className="flex flex-col gap-3 rounded-xl border border-hairline bg-surface/40 px-5 py-3.5 sm:flex-row sm:items-start sm:justify-between">
      <div className="flex min-w-0 flex-col gap-1">
        {gap.tab !== null ? (
          <Link href={hrefFor(code, gap.tab)} className={cn(LINK_CLASS, "self-start")}>
            {gap.label}
          </Link>
        ) : (
          <p className="text-sm font-medium text-foreground">{gap.label}</p>
        )}
        <p className="text-pretty text-xs leading-relaxed text-muted-foreground">{gap.why}</p>
      </div>
      {onUnderstood ? (
        <button
          type="button"
          onClick={onUnderstood}
          disabled={pending}
          aria-label={`Mark understood: ${gap.label}`}
          className={cn(BUTTON_CLASS, "shrink-0 self-start")}
        >
          {pending ? "Saving…" : "Mark understood"}
        </button>
      ) : null}
    </li>
  );
}

export function StillUnclear() {
  const { code } = useKtPackage();
  const { gaps } = useWorkspace();
  const { clear } = useProgressWrites();

  // The recipient's own flags first, then what the evidence itself lacks — the order the
  // backend emits, made explicit here so a reordering upstream cannot swap the groups.
  const yours = gaps.filter((gap) => gap.source === "you");
  const evidence = gaps.filter((gap) => gap.source !== "you");

  const clearing = (key: string) => clear.isPending && clear.variables?.key === key;

  return (
    <section aria-labelledby="kt-unclear-heading" className="flex flex-col gap-4">
      <PanelHeading id="kt-unclear-heading">Still unclear</PanelHeading>
      {yours.length === 0 && evidence.length === 0 ? (
        <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Nothing is marked unclear, and every category in scope has evidence you can read.
        </p>
      ) : (
        <div className="flex flex-col gap-5">
          {yours.length > 0 ? (
            <div className="flex flex-col gap-2">
              <h3 className="eyebrow text-muted-foreground">You marked this unclear</h3>
              <ul className="flex flex-col gap-2">
                {yours.map((gap) => (
                  <GapRow
                    key={gap.key}
                    gap={gap}
                    code={code}
                    pending={clearing(gap.key)}
                    onUnderstood={() => clear.mutate({ key: gap.key })}
                  />
                ))}
              </ul>
            </div>
          ) : null}
          {evidence.length > 0 ? (
            <div className="flex flex-col gap-2">
              <h3 className="eyebrow text-muted-foreground">Missing from the evidence</h3>
              <ul className="flex flex-col gap-2">
                {evidence.map((gap) => (
                  <GapRow key={gap.key} gap={gap} code={code} pending={false} />
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}

// -------------------------------------------------------------- learning path

function stageDone(stage: KtLearningStage): number {
  return stage.items.filter((item) => item.state === "done").length;
}

function StatePill({ state }: { state: string }) {
  const tone = state === "done" ? "good" : state === "unclear" ? "attention" : "neutral";
  return <Pill tone={tone}>{state}</Pill>;
}

function LearningItemRow({
  item,
  code,
  set,
  clear,
}: {
  item: KtLearningItem;
  code: string;
  set: ReturnType<typeof useProgressWrites>["set"];
  clear: ReturnType<typeof useProgressWrites>["clear"];
}) {
  const busy =
    (set.isPending && set.variables?.key === item.key) ||
    (clear.isPending && clear.variables?.key === item.key);

  return (
    <li className="flex flex-col gap-3 rounded-xl border border-hairline bg-surface/40 px-5 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <Link href={hrefFor(code, item.tab)} className={cn(LINK_CLASS, "min-w-0")}>
          {item.label}
        </Link>
        {item.state ? <StatePill state={item.state} /> : null}
      </div>
      <p className="text-pretty text-xs leading-relaxed text-muted-foreground">{item.why}</p>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy}
          aria-label={`Mark done: ${item.label}`}
          onClick={() => set.mutate({ key: item.key, state: "done" })}
          className={BUTTON_CLASS}
        >
          Mark done
        </button>
        <button
          type="button"
          disabled={busy}
          aria-label={`Still unclear: ${item.label}`}
          onClick={() => set.mutate({ key: item.key, state: "unclear" })}
          className={BUTTON_CLASS}
        >
          Still unclear
        </button>
        {item.state ? (
          <button
            type="button"
            disabled={busy}
            aria-label={`Clear: ${item.label}`}
            onClick={() => clear.mutate({ key: item.key })}
            className={BUTTON_CLASS}
          >
            Clear
          </button>
        ) : null}
      </div>
    </li>
  );
}

/**
 * The learning path — stages by day, each item anchored to a claim or document the
 * recipient may read.
 *
 * `compact` is the overview's summary: one row per stage and a link to the full path.
 * The full form is the learn page's body; the page supplies the h2, so stages are h3s
 * beneath it and this component adds no heading of its own there.
 */
export function LearningPath({ compact }: { compact: boolean }) {
  const { code } = useKtPackage();
  const { learning_path: stages, coverage } = useWorkspace();
  const { set, clear } = useProgressWrites();

  if (stages.length === 0) {
    return (
      <section aria-labelledby={compact ? "kt-path-heading" : undefined} className="flex flex-col gap-4">
        {compact ? <PanelHeading id="kt-path-heading">Your learning path</PanelHeading> : null}
        <EmptyState title="Not enough evidence yet">
          {/* The coverage reason explains an empty path only when coverage itself could
              not be computed. With extraction done over readable documents, an empty
              path means the categories in scope produced nothing — say that instead. */}
          <p>
            {coverage.reliable
              ? "Extraction has run over the documents you can read, but nothing in this package's scope has produced a step yet."
              : coverage.reason}
          </p>
        </EmptyState>
      </section>
    );
  }

  if (compact) {
    return (
      <section aria-labelledby="kt-path-heading" className="flex flex-col gap-4">
        <PanelHeading id="kt-path-heading">Your learning path</PanelHeading>
        <ul className="flex flex-col gap-px overflow-clip rounded-2xl border border-hairline bg-hairline">
          {stages.map((stage) => (
            <li
              key={stage.day}
              className="flex items-center justify-between gap-4 bg-background px-5 py-3.5 text-sm"
            >
              <span className="min-w-0 truncate text-foreground">
                Day {stage.day} · {stage.title}
              </span>
              <span className="shrink-0 font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground">
                {stageDone(stage)}/{stage.items.length}
              </span>
            </li>
          ))}
        </ul>
        <Link href={hrefFor(code, "learn")} className={cn(LINK_CLASS, "self-start text-brand")}>
          Open the full path
        </Link>
      </section>
    );
  }

  return (
    <div className="flex flex-col gap-8">
      {stages.map((stage) => {
        const headingId = `kt-stage-${stage.day}`;
        return (
          <section key={stage.day} aria-labelledby={headingId} className="flex flex-col gap-3">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h3 id={headingId} className="display text-lg font-semibold">
                Day {stage.day} — {stage.title}
              </h3>
              <span className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground">
                {stageDone(stage)}/{stage.items.length} done
              </span>
            </div>
            <ul className="flex flex-col gap-2">
              {stage.items.map((item) => (
                <LearningItemRow key={item.key} item={item} code={code} set={set} clear={clear} />
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
