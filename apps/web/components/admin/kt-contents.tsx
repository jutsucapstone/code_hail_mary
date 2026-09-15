"use client";

import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { EyeOff, FileText, Paperclip, RotateCcw } from "lucide-react";
import { toast } from "sonner";

import { LoadMore, Pill, When } from "@/components/admin/page-scaffold";
import { FailureState, LoadingRegion, Skeleton } from "@/components/states";
import { Button } from "@/components/ui/button";
import { api, type KtContent } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * What a package shares, for its curator to review before the KT ID travels (ADR 0027).
 *
 * A package carries its employee's own documents from their connected applications inside
 * its period, plus the Knowledge Basket files attached to it. This list is all of it, so
 * whoever creates or curates the package can keep back what must not travel — a private
 * email, a personal note — before the recipient opens it.
 *
 * **Titles, never passages.** Recognising a document needs its title; reading it is the
 * recipient's capability, and this panel does not grant it.
 *
 * **Kept back means gone everywhere the recipient looks** — Ask KT, the documents and files
 * tabs, citations, the knowledge tabs and the handover report — from their next request, because
 * the server reads the exclusion inside every one of those queries. Nothing here filters in
 * the browser.
 *
 * On a closed package a document can still be kept back but not shared again: withdrawing
 * is never the act that needs blocking, and the server refuses the other with a 409.
 */

const SOURCE_LABELS: Record<string, string> = {
  basket: "Knowledge Basket",
  confluence: "Confluence",
  github: "GitHub",
  gmail: "Google",
  jira: "Jira",
  local: "Local files",
  m365: "Microsoft 365",
  slack: "Slack",
  zoom: "Zoom",
};

export function KtContents({
  packageId,
  closed,
}: {
  packageId: string;
  /** Revoked or completed. Keeping back stays allowed; sharing again is refused. */
  closed: boolean;
}) {
  const headingId = useId();
  const queryClient = useQueryClient();
  const [older, setOlder] = useState<KtContent[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  // Distinct from `cursor === null`, which is also the state before any walk.
  const [exhausted, setExhausted] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const contents = useQuery({
    queryKey: ["kt", packageId, "contents"],
    queryFn: () => api.ktContents(packageId),
  });

  function refresh() {
    setOlder([]);
    setCursor(null);
    setExhausted(false);
    void queryClient.invalidateQueries({ queryKey: ["kt", packageId, "contents"] });
  }

  const keepBack = useMutation({
    mutationFn: (documentId: string) => api.ktExclude(packageId, documentId),
    onSuccess: () => {
      toast.success("Kept back. The recipient no longer sees it.");
      refresh();
    },
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  const shareAgain = useMutation({
    mutationFn: (documentId: string) => api.ktInclude(packageId, documentId),
    onSuccess: () => {
      toast.success("Shared again.");
      refresh();
    },
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  async function loadMore() {
    const next = cursor ?? contents.data?.next_cursor ?? null;
    if (!next) return;
    setLoadingMore(true);
    try {
      const page = await api.ktContents(packageId, { cursor: next });
      setOlder((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
      if (!page.next_cursor) setExhausted(true);
    } catch (error) {
      toast.error(classifyApiError(error).message);
    } finally {
      setLoadingMore(false);
    }
  }

  const rows = [...(contents.data?.items ?? []), ...older];
  const keptBack = rows.filter((row) => row.excluded).length;
  const hasMore = !exhausted && Boolean(cursor ?? contents.data?.next_cursor);

  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-4">
      <div>
        <h3 id={headingId} className="text-sm font-medium text-foreground">
          What this package shares
        </h3>
        <p className="mt-1.5 max-w-prose text-xs leading-relaxed text-muted-foreground">
          Everything the recipient can read once they open it: documents from this
          employee&apos;s connected applications inside the package period, and the Knowledge
          Basket files attached to it. Keep back anything that should not travel — the
          recipient stops seeing it in Ask KT, documents, files, citations and the handover
          report from their next request. Only titles are shown here.
        </p>
      </div>

      {contents.error ? (
        <FailureState
          failure={classifyApiError(contents.error)}
          onRetry={() => void contents.refetch()}
          deniedWhat="reviewing what this package shares"
        />
      ) : contents.isPending ? (
        <LoadingRegion label="Loading what this package shares.">
          <Skeleton className="h-12" />
        </LoadingRegion>
      ) : rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Nothing yet: no documents from this employee&apos;s connected applications fall
          inside the package period, and no Knowledge Basket files are attached.
        </p>
      ) : (
        <>
          <p className="text-xs text-muted-foreground">
            {rows.length === 1 ? "1 document" : `${rows.length} documents`}
            {hasMore ? " so far" : ""}
            {keptBack > 0 ? ` · ${keptBack} kept back` : ""}
          </p>
          <ul
            aria-label="Shared documents"
            className="flex max-h-96 flex-col gap-2 overflow-y-auto"
          >
            {rows.map((item) => (
              <li
                key={item.document_id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl border border-hairline bg-background px-4 py-2.5"
              >
                {item.attached_file ? (
                  <Paperclip aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
                ) : (
                  <FileText aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
                )}
                <span
                  className={`min-w-0 flex-1 truncate text-sm ${
                    item.excluded ? "text-muted-foreground" : "text-foreground"
                  }`}
                >
                  {item.title}
                </span>
                <span className="hidden text-xs text-muted-foreground sm:inline">
                  {SOURCE_LABELS[item.source_system] ?? item.source_system}
                  {item.folder_path ? ` · ${item.folder_path}` : ""}
                </span>
                <span className="hidden text-xs text-muted-foreground md:inline">
                  <When iso={item.created_at} />
                </span>
                {item.excluded ? <Pill tone="attention">Kept back</Pill> : null}
                {item.excluded ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="ml-auto"
                    disabled={
                      closed || (shareAgain.isPending && shareAgain.variables === item.document_id)
                    }
                    aria-label={`Share ${item.title} again`}
                    onClick={() => shareAgain.mutate(item.document_id)}
                  >
                    <RotateCcw aria-hidden="true" />
                    Share again
                  </Button>
                ) : (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="ml-auto text-destructive hover:text-destructive"
                    disabled={keepBack.isPending && keepBack.variables === item.document_id}
                    aria-label={`Keep ${item.title} back`}
                    onClick={() => keepBack.mutate(item.document_id)}
                  >
                    <EyeOff aria-hidden="true" />
                    Keep back
                  </Button>
                )}
              </li>
            ))}
          </ul>
          {hasMore ? <LoadMore onClick={() => void loadMore()} pending={loadingMore} /> : null}
          {closed ? (
            <p className="text-xs text-muted-foreground">
              This package is closed. Documents can still be kept back, but nothing can be
              shared again.
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}
