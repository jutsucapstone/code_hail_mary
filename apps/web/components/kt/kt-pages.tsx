"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { LoadMore, Pill, When } from "@/components/admin/page-scaffold";
import { EmptyState, FailureState, LoadingRegion, Skeleton } from "@/components/states";
import { useKtPackage } from "@/components/kt/kt-shell";
import { api, type KtDocumentPage } from "@/lib/api";
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

export function KtDocuments() {
  const { code, pkg } = useKtPackage();
  const [older, setOlder] = useState<KtDocumentPage["items"]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
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

  async function loadOlder() {
    const next = cursor ?? head.data?.next_cursor;
    if (!next) return;
    setLoadingMore(true);
    try {
      const page = await api.ktDocuments(code, { cursor: next });
      setOlder((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
    } finally {
      setLoadingMore(false);
    }
  }

  const rows = [...(head.data?.items ?? []), ...older];
  const more = cursor ?? head.data?.next_cursor;

  return (
    <div className="flex flex-col gap-6">
      <h2 className="display text-xl font-semibold">Documents</h2>
      {head.error ? (
        <FailureState
          failure={classifyApiError(head.error)}
          onRetry={() => void head.refetch()}
          deniedWhat="reading this package's documents"
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
                <span className="min-w-0">
                  <span className="block truncate text-sm text-foreground">{doc.title}</span>
                  <span className="block font-mono text-[0.625rem] uppercase tracking-[0.14em] text-muted-foreground">
                    {doc.source_system}
                  </span>
                </span>
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
