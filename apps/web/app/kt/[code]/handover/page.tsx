"use client";

import Link from "next/link";
import { useState } from "react";

import { useQuery } from "@tanstack/react-query";

import { useKtPackage } from "@/components/kt/kt-shell";
import { When } from "@/components/admin/page-scaffold";
import { FailureState } from "@/components/states";
import { api } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * Handover — what this package can hand over today, stated plainly.
 *
 * The executive summary is composed on demand from the claims THIS recipient may
 * read, grounded and citation-gated server-side exactly like Ask (§29 without a fake
 * downloadable: what renders is real, cited, and never persisted). When it cannot be
 * grounded, the refusal renders — never a fluent guess.
 */
const TYPE_LABELS: Record<string, string> = {
  decision: "decisions",
  person: "key contacts",
  project: "projects",
  meeting: "meetings",
  responsibility: "responsibilities",
};

export default function Page() {
  const { pkg, code } = useKtPackage();
  const base = `/kt/${encodeURIComponent(code)}`;
  const summary = useQuery({
    queryKey: ["kt", code, "insight-summary"],
    queryFn: () => api.ktInsightSummary(code),
  });
  const holdings = Object.entries(summary.data?.by_type ?? {})
    .map(([type, count]) => count + " " + (TYPE_LABELS[type] ?? type))
    .join(" · ");

  return (
    <div className="flex flex-col gap-6">
      <h2 className="display text-xl font-semibold">Handover</h2>
      <div className="flex flex-col gap-4 rounded-2xl border border-hairline bg-surface/40 p-8">
        <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          This package covers{" "}
          <strong className="text-foreground">
            {pkg.subject.display_name ?? "one colleague"}
          </strong>
          {pkg.subject.designation ? ` (${pkg.subject.designation})` : ""} and stays open
          until <When iso={pkg.expires_at} />. What you can act on now:
        </p>
        {holdings ? (
          <p className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-brand">
            {holdings}
          </p>
        ) : null}
        <ul className="flex flex-col gap-2 text-sm text-muted-foreground">
          <li>
            · Read the{" "}
            <Link className="text-brand underline-offset-4 hover:underline" href={`${base}/documents`}>
              documents
            </Link>{" "}
            in its window that your account is authorised to see.
          </li>
          <li>
            · Use{" "}
            <Link className="text-brand underline-offset-4 hover:underline" href={`${base}/ask`}>
              Ask KT
            </Link>{" "}
            to search that material in plain language.
          </li>
        </ul>
        <p className="max-w-prose text-pretty text-xs leading-relaxed text-muted-foreground">
          Decisions, people, projects, meetings and responsibilities in the tabs above
          are extracted from real documents, each carrying its verbatim source quote.
        </p>
      </div>

      <ExecutiveSummary code={code} />
    </div>
  );
}

function ExecutiveSummary({ code }: { code: string }) {
  const [requested, setRequested] = useState(false);
  const composed = useQuery({
    queryKey: ["kt", code, "handover-summary"],
    queryFn: () => api.ktHandoverSummary(code),
    enabled: requested,
    staleTime: Infinity,
    retry: false,
  });

  return (
    <section
      aria-labelledby="kt-exec-heading"
      className="flex flex-col gap-4 rounded-2xl border border-hairline bg-surface/40 p-8"
    >
      <h3 id="kt-exec-heading" className="display text-lg font-semibold">
        Executive summary
      </h3>
      {!requested ? (
        <>
          <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
            Compose a first-day briefing from the extracted knowledge you are authorised
            to read — responsibilities, projects, key contacts, decisions and open work,
            every claim grounded in a cited source document. Composed fresh each time,
            never stored.
          </p>
          <button
            type="button"
            onClick={() => setRequested(true)}
            className="w-fit rounded-lg bg-brand px-3.5 py-2 text-sm font-medium text-brand-foreground transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            Compose summary
          </button>
        </>
      ) : composed.error ? (
        <FailureState
          failure={classifyApiError(composed.error)}
          onRetry={() => void composed.refetch()}
          deniedWhat="composing the handover summary"
        />
      ) : composed.isPending ? (
        <p aria-live="polite" className="text-sm text-muted-foreground">
          Composing from the evidence you can read — this takes a moment.
        </p>
      ) : composed.data.insufficient_evidence || !composed.data.summary ? (
        <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          The extracted knowledge you can read is not enough to ground a summary yet.
          Nothing is generated without evidence — as extraction covers more of this
          package&apos;s documents, try again.
        </p>
      ) : (
        <>
          <div className="max-w-prose whitespace-pre-wrap text-pretty text-sm leading-relaxed text-foreground">
            {composed.data.summary}
          </div>
          <div className="flex flex-col gap-1.5 border-t border-hairline pt-4">
            <p className="font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground">
              Sources
            </p>
            <ol className="flex flex-col gap-1 text-xs text-muted-foreground">
              {composed.data.citations.map((citation) => (
                <li key={citation.marker}>
                  [{citation.marker}] {citation.document_title} ({citation.source_system})
                </li>
              ))}
            </ol>
          </div>
        </>
      )}
    </section>
  );
}
