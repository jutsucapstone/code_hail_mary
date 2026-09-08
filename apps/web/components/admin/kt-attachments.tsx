"use client";

import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Paperclip, X } from "lucide-react";
import { toast } from "sonner";

import { Pill } from "@/components/admin/page-scaffold";
import { EmptyState, FailureState, LoadingRegion, Skeleton } from "@/components/states";
import { Button } from "@/components/ui/button";
import { api, type KtAttachment } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * Choosing which of the departing employee's files travel with this package (ADR 0021).
 *
 * **Attaching is a reference, not a grant.** The recipient's access is recomputed from
 * the package's live state on every request, so revoking the package closes these files
 * with nothing happening here — and detaching one withdraws it immediately, without
 * touching the file, which stays the employee's.
 *
 * The list on the left is bounded server-side by exactly the conditions the write
 * enforces, so it cannot offer something the attach would then refuse. A curator who
 * holds `kt:manage` but not `basket:manage` sees an empty list rather than a filtered
 * view of somebody else's basket — which is the least-privilege answer and the reason
 * this panel says so in words rather than rendering a blank.
 */

const STATE_TONE: Record<string, "good" | "attention" | "neutral" | "bad"> = {
  ready: "good",
  stored: "neutral",
  failed: "bad",
};

const STATE_LABEL: Record<string, string> = {
  uploaded: "Processing",
  validating: "Processing",
  extracting: "Processing",
  chunking: "Processing",
  embedding: "Processing",
  ready: "Searchable",
  stored: "Stored",
  failed: "Text unavailable",
};

function readableSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function FileRow({
  file,
  children,
}: {
  file: KtAttachment;
  children: React.ReactNode;
}) {
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl border border-hairline bg-background px-4 py-2.5">
      <FileText aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate text-sm text-foreground">{file.filename}</span>
      <span className="hidden text-xs tabular-nums text-muted-foreground sm:inline">
        {readableSize(file.size_bytes)}
      </span>
      <Pill tone={STATE_TONE[file.state] ?? "attention"}>
        {STATE_LABEL[file.state] ?? file.state}
      </Pill>
      {children}
    </li>
  );
}

export function KtAttachments({
  packageId,
  closed,
}: {
  packageId: string;
  /** Revoked or completed. Attaching is refused server-side; detaching stays allowed. */
  closed: boolean;
}) {
  const headingId = useId();
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const queryClient = useQueryClient();

  const attached = useQuery({
    queryKey: ["kt", packageId, "attachments"],
    queryFn: () => api.ktAttachments(packageId),
  });
  const available = useQuery({
    queryKey: ["kt", packageId, "attachable"],
    queryFn: () => api.ktAttachable(packageId),
    // Pointless on a closed package: the write would be refused anyway, and offering a
    // picker that cannot succeed is the "fake button" §24 forbids.
    enabled: !closed,
  });

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ["kt", packageId, "attachments"] });
    void queryClient.invalidateQueries({ queryKey: ["kt", packageId, "attachable"] });
  }

  const attach = useMutation({
    mutationFn: (ids: string[]) => api.ktAttach(packageId, ids),
    onSuccess: ({ attached: count }, ids) => {
      setChosen(new Set());
      refresh();
      // The server's count, not the request's length. Fewer means something was skipped
      // — already attached, not the subject's, or not visible to this curator — and
      // saying "5 attached" when 3 landed is the kind of lie that is found much later.
      if (count === ids.length) {
        toast.success(count === 1 ? "One file attached." : `${count} files attached.`);
      } else {
        toast.warning(
          `${count} of ${ids.length} attached. The rest were already on this package or are not this employee's.`,
        );
      }
    },
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  const detach = useMutation({
    mutationFn: (fileId: string) => api.ktDetach(packageId, fileId),
    onSuccess: () => {
      toast.success("That file is no longer shared.");
      refresh();
    },
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  function toggle(fileId: string) {
    setChosen((current) => {
      const next = new Set(current);
      if (next.has(fileId)) next.delete(fileId);
      else next.add(fileId);
      return next;
    });
  }

  const offered = available.data?.items ?? [];
  const live = attached.data?.items ?? [];

  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-4">
      <div>
        <h3 id={headingId} className="text-sm font-medium text-foreground">
          Shared Knowledge Basket files
        </h3>
        <p className="mt-1.5 max-w-prose text-xs leading-relaxed text-muted-foreground">
          Files from this employee&apos;s own Knowledge Basket that travel with the
          package. The recipient sees these and nothing else from their basket. Revoking
          or completing the package closes them; detaching one withdraws it immediately.
          The file itself is never moved or copied and stays the employee&apos;s.
        </p>
      </div>

      {/* --------------------------------------------------------- already attached */}
      {attached.error ? (
        <FailureState
          failure={classifyApiError(attached.error)}
          onRetry={() => void attached.refetch()}
          deniedWhat="managing this package's files"
        />
      ) : attached.isPending ? (
        <LoadingRegion label="Loading attached files.">
          <Skeleton className="h-12" />
        </LoadingRegion>
      ) : live.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No files are attached. The handover will be built from connected applications
          only.
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {live.map((file) => (
            <FileRow key={file.id} file={file}>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="ml-auto text-destructive hover:text-destructive"
                disabled={detach.isPending && detach.variables === file.file_id}
                aria-label={`Stop sharing ${file.filename}`}
                onClick={() => detach.mutate(file.file_id)}
              >
                <X aria-hidden="true" />
                Stop sharing
              </Button>
            </FileRow>
          ))}
        </ul>
      )}

      {/* ------------------------------------------------------------- the picker
          Skipped entirely when the attached list failed: the two queries hit the same
          endpoint family and fail together, so rendering both errors puts two identical
          panels on the screen — and a picker is meaningless when what is already shared
          could not be read. */}
      {attached.error ? null : closed ? (
        <p className="text-xs text-muted-foreground">
          This package is closed, so no further files can be attached. Withdrawing one is
          still possible.
        </p>
      ) : available.error ? (
        <FailureState
          failure={classifyApiError(available.error)}
          onRetry={() => void available.refetch()}
          deniedWhat="reading this employee's Knowledge Basket"
        />
      ) : available.isPending ? (
        <LoadingRegion label="Loading files that can be attached.">
          <Skeleton className="h-12" />
        </LoadingRegion>
      ) : offered.length === 0 ? (
        <EmptyState title="Nothing else to attach">
          <p className="text-xs leading-relaxed">
            Either this employee has uploaded nothing further, everything is already
            attached, or your role does not include reading another employee&apos;s
            Knowledge Basket. Seeing someone else&apos;s uploads requires Knowledge Basket
            management, which organisation owners, super admins and IT admins hold.
          </p>
        </EmptyState>
      ) : (
        <div className="flex flex-col gap-3 rounded-2xl border border-hairline-strong bg-surface/30 p-4">
          <p className="text-xs font-medium text-foreground">
            Add from {offered.length === 1 ? "1 file" : `${offered.length} files`} in their
            basket
          </p>
          <ul className="flex max-h-72 flex-col gap-2 overflow-y-auto">
            {offered.map((file) => (
              <FileRow key={file.file_id} file={file}>
                {/* `aria-label` rather than a visually-hidden span beside "Share": the
                    span would put the filename in the DOM twice, which reads as a
                    stutter to a screen reader moving by text and makes the row
                    ambiguous to anything matching on it. */}
                <label className="ml-auto flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    aria-label={`Share ${file.filename}`}
                    checked={chosen.has(file.file_id)}
                    onChange={() => toggle(file.file_id)}
                    className="size-4 rounded border-hairline-strong accent-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                  />
                  <span aria-hidden="true">Share</span>
                </label>
              </FileRow>
            ))}
          </ul>
          <Button
            type="button"
            size="sm"
            className="self-start"
            disabled={chosen.size === 0 || attach.isPending}
            onClick={() => attach.mutate([...chosen])}
          >
            <Paperclip aria-hidden="true" />
            {chosen.size === 0
              ? "Choose files to share"
              : chosen.size === 1
                ? "Share 1 file"
                : `Share ${chosen.size} files`}
          </Button>
        </div>
      )}
    </section>
  );
}
