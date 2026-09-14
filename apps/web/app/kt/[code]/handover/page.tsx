"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Download, ExternalLink, FileText, Loader2 } from "lucide-react";

import { KtFailure } from "@/components/kt/kt-failure";
import { useKtPackage } from "@/components/kt/kt-shell";
import { When } from "@/components/admin/page-scaffold";
import { api, type KtHandoverReport } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * Handover — what this package hands over, stated plainly.
 *
 * Everything on this page is the package SUBJECT's knowledge (ADR 0025): their documents,
 * their extracted claims, and a first-day summary composed from those claims on demand.
 * The summary is grounded and citation-gated server-side exactly like Ask, and it arrives
 * together with the same report rendered as a real PDF — one response, so the text on
 * screen and the file downloaded are one composition, never two model calls that could
 * disagree. Nothing is persisted: the PDF lives in this tab until the recipient saves it.
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

  /* How to describe the person whose context this is.
   *
   * The assigned title is preferred over the free-text `designation` because it is the
   * one the organisation stands behind, and the normalized level is appended because it
   * is the part a recipient from another practice can actually interpret — "Audit
   * Senior" means little to an engineer; "Audit Senior · Senior Consultant" places them.
   * Both arrive only when the package's scope includes the profile, so an empty string
   * here is a scope decision rather than missing data. */
  const subjectRole =
    [pkg.subject.role_title ?? pkg.subject.designation, pkg.subject.role_level]
      .filter(Boolean)
      .join(" · ") || null;
  const subjectName = pkg.subject.display_name ?? "your colleague";

  return (
    <div className="flex flex-col gap-6">
      <h2 className="display text-xl font-semibold">Handover</h2>
      <div className="flex flex-col gap-4 rounded-2xl border border-hairline bg-surface/40 p-8">
        <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          This package covers{" "}
          <strong className="text-foreground">
            {pkg.subject.display_name ?? "one colleague"}
          </strong>
          {subjectRole ? ` (${subjectRole})` : ""} and stays open until{" "}
          <When iso={pkg.expires_at} />. What you can act on now:
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
            from {subjectName}&apos;s connected accounts and Knowledge Basket inside this
            package&apos;s window.
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
          are extracted from those documents, each carrying its verbatim source quote.
        </p>
      </div>

      <ExecutiveSummary code={code} subjectName={subjectName} />
    </div>
  );
}

/** The PDF bytes the API sent as base64, as a Blob a browser can open or save. */
function pdfBlob(base64: string): Blob {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Blob([bytes], { type: "application/pdf" });
}

const PRIMARY =
  "inline-flex w-fit items-center gap-2 rounded-lg bg-brand px-3.5 py-2 text-sm font-medium text-brand-foreground transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand";
const SECONDARY =
  "inline-flex w-fit items-center gap-2 rounded-lg border border-hairline-strong px-3 py-1.5 text-sm font-medium text-foreground transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand";

/**
 * Compose summary: one press, one POST, one model call — the narrative and its PDF.
 *
 * A mutation, not a query: composing spends a `KT_SUMMARY` allowance and writes an audit
 * row, so it must run exactly when pressed and never again because a panel re-mounted or
 * the window regained focus. The button is disabled while a composition is in flight, so
 * a double click is one composition, not two charged ones.
 *
 * The PDF is held as an object URL for this tab only, released when a new composition
 * starts and when the page unmounts — a blob URL that is never revoked keeps the whole
 * report in memory for the life of the tab.
 */
function ExecutiveSummary({ code, subjectName }: { code: string; subjectName: string }) {
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const urlRef = useRef<string | null>(null);

  const release = () => {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    urlRef.current = null;
    setPdfUrl(null);
  };

  useEffect(
    () => () => {
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    },
    [],
  );

  const compose = useMutation<KtHandoverReport>({
    mutationFn: () => api.ktHandoverReport(code),
    onMutate: release,
    onSuccess: (report) => {
      const url = URL.createObjectURL(pdfBlob(report.pdf_base64));
      urlRef.current = url;
      setPdfUrl(url);
    },
  });

  const start = () => {
    if (compose.isPending) return;
    compose.mutate();
  };

  const button = (
    <button type="button" onClick={start} disabled={compose.isPending} className={PRIMARY}>
      {compose.isPending ? (
        <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin motion-reduce:animate-none" />
      ) : (
        <FileText aria-hidden="true" className="h-4 w-4" />
      )}
      {compose.isPending ? "Composing…" : compose.isSuccess ? "Compose again" : "Compose summary"}
    </button>
  );

  return (
    <section
      aria-labelledby="kt-exec-heading"
      aria-busy={compose.isPending}
      className="flex flex-col gap-4 rounded-2xl border border-hairline bg-surface/40 p-8"
    >
      <h3 id="kt-exec-heading" className="display text-lg font-semibold">
        Executive summary
      </h3>

      {compose.isIdle ? (
        <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Compose a first-day briefing from {subjectName}&apos;s knowledge in this package —
          responsibilities, projects, key contacts, decisions and open work, every statement
          grounded in a cited source. You get it here and as a PDF to keep. Composed fresh
          each time; JUTSU stores neither.
        </p>
      ) : null}

      {compose.isPending ? (
        <p aria-live="polite" className="text-sm text-muted-foreground">
          Composing from {subjectName}&apos;s knowledge and preparing the PDF — this takes a
          moment.
        </p>
      ) : null}

      {compose.isError
        ? (() => {
            const failure = classifyApiError(compose.error);
            // A 503 is the deployment saying no answer model is configured, or that none
            // could answer just now — the API's own sentence says which. A generic "That
            // did not load" with a Try again control would describe a fault in the page;
            // the button below stays so the recipient can press again when they choose.
            if (failure.kind === "unavailable") {
              return (
                <p
                  role="alert"
                  className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground"
                >
                  {failure.message}
                </p>
              );
            }
            return <KtFailure failure={failure} onRetry={start} />;
          })()
        : null}

      {compose.isSuccess ? <Composed report={compose.data} pdfUrl={pdfUrl} /> : null}

      {compose.isError && classifyApiError(compose.error).kind !== "unavailable" ? null : button}
    </section>
  );
}

function Composed({ report, pdfUrl }: { report: KtHandoverReport; pdfUrl: string | null }) {
  const grounded = !report.insufficient_evidence && Boolean(report.summary);
  return (
    <div className="flex flex-col gap-4">
      {grounded ? (
        <>
          <div className="max-w-prose whitespace-pre-wrap text-pretty text-sm leading-relaxed text-foreground">
            {report.summary}
          </div>
          <div className="flex flex-col gap-1.5 border-t border-hairline pt-4">
            <p className="font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground">
              Sources
            </p>
            <ol className="flex flex-col gap-1 text-xs text-muted-foreground">
              {report.references.map((reference) => (
                <li key={reference.number}>
                  [{reference.number}] {reference.document_title} ({reference.source_system})
                </li>
              ))}
            </ol>
          </div>
        </>
      ) : (
        <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          The extracted knowledge in this package is not enough to ground a summary yet.
          Nothing is generated without evidence — the PDF still lists what the package holds,
          section by section.
        </p>
      )}

      {pdfUrl ? (
        <div className="flex flex-wrap items-center gap-3">
          <a href={pdfUrl} download={report.filename} className={SECONDARY}>
            <Download aria-hidden="true" className="h-4 w-4" />
            Download PDF
          </a>
          <a href={pdfUrl} target="_blank" rel="noopener noreferrer" className={SECONDARY}>
            <ExternalLink aria-hidden="true" className="h-4 w-4" />
            Open PDF
          </a>
        </div>
      ) : null}
    </div>
  );
}
