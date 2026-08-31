"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { GitBranch } from "lucide-react";

import { LoadMore, Pill, When } from "@/components/admin/page-scaffold";
import { EmptyState, FailureState, LoadingRegion, Skeleton } from "@/components/states";
import { useKtPackage } from "@/components/kt/kt-shell";
import { api, type KtDocumentPage } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * The KT console's pages, sharing the package context the shell established.
 *
 * One honesty rule runs through all of them: a tab either renders REAL data from a real
 * endpoint, or it says precisely which capability the platform is missing (§36). The
 * graph tabs — projects, decisions, meetings, people, responsibilities, timeline — are
 * navigable because a recipient should see the shape of what a package will hold, and
 * they say "requires knowledge-graph extraction" because that is the truth.
 */

const SCOPE_LABELS: Record<string, string> = {
  documents: "Documents",
  profile: "Role & profile",
};

export function KtOverview() {
  const { pkg } = useKtPackage();

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

/**
 * A tab whose data source does not exist on this deployment yet.
 *
 * Navigable on purpose — the recipient should see the shape of what a package can hold
 * — and honest on purpose: the sentence names the missing capability rather than
 * showing an empty table that reads as "nothing happened" (§36).
 */
export function KtCapabilityGate({ name, what }: { name: string; what: string }) {
  return (
    <div className="flex flex-col gap-6">
      <h2 className="display text-xl font-semibold">{name}</h2>
      <div className="flex flex-col items-start gap-4 rounded-2xl border border-hairline bg-surface/40 p-8">
        <span
          aria-hidden="true"
          className="flex size-10 items-center justify-center rounded-xl border border-hairline-strong bg-surface text-brand"
        >
          <GitBranch className="size-5" />
        </span>
        <div>
          <h3 className="display text-lg font-semibold">Waiting on the knowledge graph</h3>
          <p className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
            {what} come from knowledge-graph extraction, which this deployment has not
            run yet. When extraction lands, packages include this section automatically —
            nothing here will ever be an invented placeholder.
          </p>
        </div>
      </div>
    </div>
  );
}
