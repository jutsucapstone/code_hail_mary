"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Download, FileText } from "lucide-react";
import { toast } from "sonner";

import { Pill, When } from "@/components/admin/page-scaffold";
import { KtFailure } from "@/components/kt/kt-failure";
import { useKtPackage } from "@/components/kt/kt-shell";
import { EmptyState, LoadingRegion, Skeleton } from "@/components/states";
import { Button } from "@/components/ui/button";
import { api, type KtFile } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * The Knowledge Basket files shared with this package (ADR 0021).
 *
 * **Not the subject's basket.** Only what somebody curating this handover explicitly
 * attached reaches this list; the rest of the departing employee's uploads are invisible
 * here and stay invisible. Nothing on this page can widen that — the server re-decides the
 * grant from the package's live state on every request, so a package revoked between two
 * clicks refuses the second one.
 *
 * The `searchable` distinction is carried through in the owner's own words rather than
 * flattened: a recipient must not be told a recording is searchable when nothing in this
 * deployment transcribes one, and must not be left guessing why a document they can
 * download never comes back from Ask KT.
 */

/** The owner's state vocabulary, reused verbatim so two consoles cannot disagree. */
const STATES: Record<string, { label: string; tone: "good" | "attention" | "neutral" | "bad" }> = {
  uploaded: { label: "Processing", tone: "attention" },
  validating: { label: "Processing", tone: "attention" },
  extracting: { label: "Processing", tone: "attention" },
  chunking: { label: "Processing", tone: "attention" },
  embedding: { label: "Processing", tone: "attention" },
  ready: { label: "Searchable", tone: "good" },
  stored: { label: "Stored", tone: "neutral" },
  failed: { label: "Text unavailable", tone: "bad" },
};

function shown(state: string) {
  return STATES[state] ?? { label: state, tone: "neutral" as const };
}

/** Every state's one-line explanation, so nothing is a bare label. */
function explain(file: KtFile): string {
  if (file.state === "ready") {
    return "Its text is in the corpus, so Ask KT can quote and cite it.";
  }
  if (file.state === "stored") {
    return "Kept and downloadable. This kind of file is not read for text, so Ask KT cannot quote it.";
  }
  if (file.state === "failed") {
    return "The file is intact and downloadable — its text could not be read, so Ask KT cannot quote it.";
  }
  return "Still being read. Downloading works now; searching will once it finishes.";
}

function readableSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function KtFiles() {
  const { code } = useKtPackage();

  const files = useQuery({
    queryKey: ["kt", code, "files"],
    // No `staleTime`: an attachment withdrawn, or a package revoked, must stop rendering
    // on the next request rather than after a cache window (§39).
    staleTime: 0,
    queryFn: () => api.ktFiles(code),
  });

  const download = useMutation({
    mutationFn: (fileId: string) => api.ktFileDownloadUrl(code, fileId),
    // Fetched at the moment of the press, never carried in the listing: the URL is
    // short-lived and is minted only after the grant is verified again.
    onSuccess: ({ url }) => window.location.assign(url),
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  const rows = files.data?.items ?? [];

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h2 className="display text-xl font-semibold">Shared files</h2>
        <p className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Files the departing employee&apos;s administrator chose to include in this
          handover. This is not their whole Knowledge Basket — only what was attached here.
        </p>
      </div>

      {files.error ? (
        <KtFailure
          failure={classifyApiError(files.error)}
          onRetry={() => void files.refetch()}
        />
      ) : files.isPending ? (
        <LoadingRegion label="Loading shared files.">
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((n) => (
              <Skeleton key={n} className="h-16" />
            ))}
          </div>
        </LoadingRegion>
      ) : rows.length === 0 ? (
        <EmptyState title="No files were attached to this package">
          <p>
            A handover can be built entirely from connected applications, so an empty list
            here is normal rather than a fault. If you were expecting a specific document,
            ask the administrator who created this package to attach it.
          </p>
        </EmptyState>
      ) : (
        <ul className="flex flex-col gap-2">
          {rows.map((file) => (
            <li
              key={file.id}
              className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-hairline bg-surface/40 px-4 py-3"
            >
              <FileText aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                {file.filename}
              </span>
              <span className="hidden text-xs tabular-nums text-muted-foreground sm:inline">
                {readableSize(file.size_bytes)}
              </span>
              <span className="hidden text-xs text-muted-foreground md:inline">
                <When iso={file.attached_at} />
              </span>
              <Pill tone={shown(file.state).tone}>{shown(file.state).label}</Pill>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={download.isPending && download.variables === file.id}
                aria-label={`Download ${file.filename}`}
                onClick={() => download.mutate(file.id)}
                className="ml-auto"
              >
                <Download aria-hidden="true" />
                Download
              </Button>
              <p className="w-full text-xs leading-relaxed text-muted-foreground">
                {explain(file)}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
