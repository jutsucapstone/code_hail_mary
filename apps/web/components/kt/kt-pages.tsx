"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";

import { LoadMore, Pill, When } from "@/components/admin/page-scaffold";
import { EmptyState, LoadingRegion, Skeleton } from "@/components/states";
import { KtFailure } from "@/components/kt/kt-failure";
import { useKtPackage } from "@/components/kt/kt-shell";
import {
  CoveragePanel,
  LearningPath,
  Recommended,
  ResumeCard,
  StillUnclear,
  WorkspaceRegion,
} from "@/components/kt/kt-workspace";
import { api, type KtDocumentPage } from "@/lib/api";
import type { paths } from "@/lib/api-schema";
import { classifyApiError } from "@/lib/api-error";

/**
 * The KT console's pages, sharing the package context the shell established.
 *
 * One honesty rule runs through all of them: a tab either renders REAL data from a real
 * endpoint, or it says precisely why it is empty (§36). The knowledge tabs — projects,
 * decisions, meetings, people, responsibilities, timeline — live in kt-insights.tsx and
 * are served from evidence-anchored extraction claims under the recipient's own ACL.
 */

const SCOPE_LABELS: Record<string, string> = {
  documents: "Documents",
  profile: "Role & profile",
  decisions: "Decisions",
  people: "Key contacts",
  projects: "Projects",
  meetings: "Meetings",
  responsibilities: "Responsibilities",
};

const TYPE_LABELS: Record<string, string> = {
  decision: "Decisions",
  person: "People",
  project: "Projects",
  meeting: "Meetings",
  responsibility: "Responsibilities",
};

export function KtOverview() {
  const { pkg, code } = useKtPackage();
  const summary = useQuery({
    queryKey: ["kt", code, "insight-summary"],
    queryFn: () => api.ktInsightSummary(code),
  });

  return (
    <div className="flex flex-col gap-8">
      {/* The personalised region: one query, its states owned by WorkspaceRegion so the
          package facts below still render from `pkg` whatever the workspace call did. */}
      <WorkspaceRegion>
        <ResumeCard />
        <div className="grid gap-8 lg:grid-cols-2">
          <Recommended />
          <CoveragePanel />
        </div>
        <StillUnclear />
        <LearningPath compact />
      </WorkspaceRegion>

      <section aria-labelledby="kt-about-heading" className="flex flex-col gap-4">
        <h2 id="kt-about-heading" className="display text-xl font-semibold">
          About this package
        </h2>
        <dl className="grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-2">
          <div className="flex flex-col gap-1.5 bg-background p-5">
            <dt className="text-sm text-muted-foreground">Knowledge scope</dt>
            <dd className="flex flex-wrap gap-1.5">
              {pkg.scope.map((category) => (
                <Pill key={category} tone="neutral">
                  {SCOPE_LABELS[category] ?? category}
                </Pill>
              ))}
            </dd>
          </div>
          <div className="flex flex-col gap-1.5 bg-background p-5">
            <dt className="text-sm text-muted-foreground">Knowledge period</dt>
            <dd className="text-sm text-foreground">
              {pkg.period_start ? (
                <>
                  <When iso={pkg.period_start} /> — <When iso={pkg.period_end} />
                </>
              ) : (
                "Full history"
              )}
            </dd>
          </div>
          <div className="flex flex-col gap-1.5 bg-background p-5">
            <dt className="text-sm text-muted-foreground">Access expires</dt>
            <dd className="text-sm text-foreground">
              <When iso={pkg.expires_at} />
            </dd>
          </div>
          <div className="flex flex-col gap-1.5 bg-background p-5">
            <dt className="text-sm text-muted-foreground">Created</dt>
            <dd className="text-sm text-foreground">
              <When iso={pkg.created_at} />
            </dd>
          </div>
        </dl>
      </section>

      {pkg.scope.includes("profile") ? (
        <section aria-labelledby="kt-subject-heading" className="flex flex-col gap-4">
          <h2 id="kt-subject-heading" className="display text-xl font-semibold">
            Who this is about
          </h2>
          <dl className="grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-3">
            <div className="flex flex-col gap-1.5 bg-background p-5">
              <dt className="text-sm text-muted-foreground">Name</dt>
              <dd className="text-sm text-foreground">
                {pkg.subject.display_name ?? "Not recorded"}
              </dd>
            </div>
            <div className="flex flex-col gap-1.5 bg-background p-5">
              <dt className="text-sm text-muted-foreground">Role</dt>
              <dd className="text-sm text-foreground">
                {pkg.subject.designation ?? "Not recorded"}
              </dd>
            </div>
            <div className="flex flex-col gap-1.5 bg-background p-5">
              <dt className="text-sm text-muted-foreground">Team</dt>
              <dd className="text-sm text-foreground">
                {pkg.subject.department ?? "Not recorded"}
              </dd>
            </div>
          </dl>
        </section>
      ) : null}

      {summary.data && Object.keys(summary.data.by_type).length > 0 ? (
        <section aria-labelledby="kt-counts-heading" className="flex flex-col gap-4">
          <h2 id="kt-counts-heading" className="display text-xl font-semibold">
            What this package holds for you
          </h2>
          {/* Counts computed under the same ACL predicate that serves the rows — a
              figure here can never exceed what its tab would show. */}
          <dl className="grid grid-cols-2 gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-5">
            {Object.entries(summary.data.by_type).map(([type, count]) => (
              <div key={type} className="flex flex-col gap-1 bg-background p-4">
                <dd className="display text-2xl font-semibold tabular-nums">{count}</dd>
                <dt className="text-xs text-muted-foreground">{TYPE_LABELS[type] ?? type}</dt>
              </div>
            ))}
          </dl>
        </section>
      ) : null}

      <section className="rounded-2xl border border-hairline bg-surface/40 p-6">
        <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Start with <strong className="text-foreground">Documents</strong> for the
          material inside this package&apos;s window, or{" "}
          <strong className="text-foreground">Ask KT</strong> to search it in plain
          language. Everything you see here is bounded by what your own account is
          authorised to read — this package widens nothing.
        </p>
      </section>
    </div>
  );
}

/**
 * The reader's payload, straight from the generated schema.
 *
 * It was hand-written while the route and this component landed in the same run and the
 * schema had not been regenerated yet. Both are in it now, so the type comes from the
 * contract rather than from a copy of it — non-negotiable 13, and the reason a renamed
 * field becomes a compile error here instead of `undefined` on the page.
 */
type KtDocumentDetail =
  paths["/v1/kt/{kt_code}/documents/{document_id}"]["get"]["responses"][200]["content"]["application/json"];

/**
 * One document, opened.
 *
 * The server decided everything that matters before a word of this arrived: the package
 * is open, the document is inside its period, and the recipient's own grants cover it.
 * A document failing any of those is the same 404 as one that never existed, which is
 * why "no longer available to you" is the honest heading for that case rather than an
 * error — nothing here is broken.
 */
function KtDocumentReader({ documentId, onBack }: { documentId: string; onBack: () => void }) {
  const { code } = useKtPackage();
  const [rest, setRest] = useState<KtDocumentDetail["chunks"]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  // Distinct from `cursor === null`, which is also the state before any walk — the same
  // trap the listing below documents.
  const [exhausted, setExhausted] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const head = useQuery({
    queryKey: ["kt", code, "document", documentId],
    queryFn: () => api.ktDocument(code, documentId),
  });

  async function loadMore() {
    const next = cursor ?? head.data?.next_ordinal ?? null;
    if (next === null) return;
    setLoadingMore(true);
    try {
      const page = await api.ktDocument(code, documentId, { fromOrdinal: next });
      setRest((current) => [...current, ...page.chunks]);
      setCursor(page.next_ordinal);
      if (page.next_ordinal === null) setExhausted(true);
    } catch (error) {
      toast.error(classifyApiError(error).message);
    } finally {
      setLoadingMore(false);
    }
  }

  const failure = head.error ? classifyApiError(head.error) : null;
  const passages = [...(head.data?.chunks ?? []), ...rest];
  const more = !exhausted && (cursor ?? head.data?.next_ordinal ?? null) !== null;

  return (
    <div className="flex flex-col gap-6">
      {/* Rendered in every state, including the failed ones. A reader who cannot get back
          to the list has only the browser's back button, and the tab is a client route. */}
      <button
        type="button"
        onClick={onBack}
        className="self-start rounded-lg border border-hairline-strong px-3.5 py-2 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
      >
        Back to documents
      </button>

      {failure ? (
        failure.kind === "missing" ? (
          <EmptyState title="This document is no longer available to you">
            <p>
              {failure.message} A document leaves this view when your access to it is
              revoked, when it is superseded by a newer version, or when it falls outside
              the package&apos;s period. Nothing failed — go back to the list.
            </p>
          </EmptyState>
        ) : (
          <KtFailure failure={failure} onRetry={() => void head.refetch()} />
        )
      ) : head.isPending ? (
        <LoadingRegion label="Loading the document.">
          <div className="flex flex-col gap-3">
            <Skeleton className="h-8 w-2/3" />
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-20" />
            ))}
          </div>
        </LoadingRegion>
      ) : head.data ? (
        <article className="flex flex-col gap-6">
          <header className="flex flex-col gap-2">
            <h2 className="display text-xl font-semibold">{head.data.title}</h2>
            <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span className="font-mono uppercase tracking-[0.14em]">
                {head.data.source_system}
              </span>
              <When iso={head.data.created_at} />
              <span>
                {passages.length} of {head.data.total_chunks}{" "}
                {head.data.total_chunks === 1 ? "passage" : "passages"}
              </span>
            </p>
          </header>

          {passages.length === 0 ? (
            <EmptyState title="Nothing to read in this document yet">
              <p>
                You are authorised to read this document and it sits inside the
                package&apos;s period, but no passages have been stored for it. Text
                becomes readable here once ingestion has chunked the document.
              </p>
            </EmptyState>
          ) : (
            <>
              <div className="flex flex-col gap-4 rounded-2xl border border-hairline bg-surface/40 p-6">
                {passages.map((passage) => (
                  <p
                    key={passage.ordinal}
                    className="max-w-prose whitespace-pre-wrap text-pretty text-sm leading-relaxed text-foreground"
                  >
                    {passage.text}
                  </p>
                ))}
              </div>
              {/* Said plainly, because a reader meeting `[EMAIL_A7]` for the first time
                  otherwise reads it as corruption. */}
              <p className="text-xs text-muted-foreground">
                Shown as stored: addresses, phone numbers and similar identifiers appear as
                masked tokens.
              </p>
            </>
          )}

          {more ? <LoadMore onClick={() => void loadMore()} pending={loadingMore} /> : null}
        </article>
      ) : null}
    </div>
  );
}

export function KtDocuments() {
  const { code, pkg } = useKtPackage();
  const [openId, setOpenId] = useState<string | null>(null);
  const [older, setOlder] = useState<KtDocumentPage["items"]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  // Distinct from `cursor === null`, which is also the state before any walk: without
  // it the null cursor falls back to the head page's cursor and the walk restarts.
  const [exhausted, setExhausted] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const inScope = pkg.scope.includes("documents");
  const head = useQuery({
    queryKey: ["kt", code, "documents"],
    queryFn: () => api.ktDocuments(code),
    enabled: inScope,
  });

  if (!inScope) {
    return (
      <EmptyState title="Documents are not part of this package">
        <p>The administrator scoped this package to: {pkg.scope.join(", ")}.</p>
      </EmptyState>
    );
  }

  if (openId !== null) {
    return <KtDocumentReader documentId={openId} onBack={() => setOpenId(null)} />;
  }

  async function loadOlder() {
    const next = cursor ?? head.data?.next_cursor;
    if (!next) return;
    setLoadingMore(true);
    try {
      const page = await api.ktDocuments(code, { cursor: next });
      setOlder((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
      if (page.next_cursor === null) setExhausted(true);
    } catch (error) {
      toast.error(classifyApiError(error).message);
    } finally {
      setLoadingMore(false);
    }
  }

  const rows = [...(head.data?.items ?? []), ...older];
  const more = !exhausted && (cursor ?? head.data?.next_cursor);

  return (
    <div className="flex flex-col gap-6">
      <h2 className="display text-xl font-semibold">Documents</h2>
      {head.error ? (
        <KtFailure
          failure={classifyApiError(head.error)}
          onRetry={() => void head.refetch()}
        />
      ) : head.isPending ? (
        <LoadingRegion label="Loading documents.">
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-14" />
            ))}
          </div>
        </LoadingRegion>
      ) : rows.length === 0 ? (
        <EmptyState title="Nothing you are authorised to read in this window">
          <p>
            Documents appear here when your account holds read access to material inside
            the package&apos;s period. Access comes from your linked source identities —
            if you expected more, ask your administrator which accounts are linked for
            you. The package itself cannot widen what you may read.
          </p>
        </EmptyState>
      ) : (
        <>
          <ul className="flex flex-col gap-2">
            {rows.map((doc) => (
              <li
                key={doc.id}
                className="flex items-center justify-between gap-4 rounded-xl border border-hairline bg-surface/40 px-5 py-3.5"
              >
                <button
                  type="button"
                  onClick={() => setOpenId(doc.id)}
                  className="min-w-0 flex-1 rounded text-left focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand"
                >
                  <span className="block truncate text-sm text-foreground underline-offset-4 hover:underline">
                    {doc.title}
                  </span>
                  <span className="block font-mono text-[0.625rem] uppercase tracking-[0.14em] text-muted-foreground">
                    {doc.source_system}
                  </span>
                </button>
                <span className="shrink-0 text-xs text-muted-foreground">
                  <When iso={doc.created_at} />
                </span>
              </li>
            ))}
          </ul>
          {more ? <LoadMore onClick={() => void loadOlder()} pending={loadingMore} /> : null}
        </>
      )}
    </div>
  );
}
