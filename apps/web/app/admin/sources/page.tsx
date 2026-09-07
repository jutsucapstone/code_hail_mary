"use client";

import { useMemo, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { useCapabilities } from "@/components/admin/admin-shell";
import { PageHeader, Pill, TableShell, When } from "@/components/admin/page-scaffold";
import {
  EmptyState,
  FailureState,
  LoadingRegion,
  PermissionDenied,
  Skeleton,
} from "@/components/states";
import { api } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import { can } from "@/lib/permissions";

/**
 * Knowledge sources — what has been connected, what it has produced, and one way to act.
 *
 * Shows sync *state*, never configuration *content*: the API withholds `config_json`
 * because corpus paths and connector settings describe infrastructure nobody needs on a
 * health row. Document counts are current versions only — superseded versions are
 * history, not inventory.
 *
 * Re-sync is a single click with no confirmation, because the act is idempotent by
 * construction: the server holds one walk per source, so a second click while one is
 * queued names the job already running rather than starting a rival. A confirmation
 * dialog would be ceremony over something that cannot be done twice.
 *
 * Nothing here optimistically flips the row to "syncing". The worker owns
 * `sources.status`, and a state written from the browser that nothing ever clears is a
 * faked surface (§4.11) — the queued walk shows up in the counters this page already
 * reads, once it has actually happened.
 */

/**
 * What this source actually is, in the words the person who connected it would use.
 *
 * `source.system` is the ACL namespace, not the provider: Drive, Gmail, Calendar and
 * Meet are all `gmail`, and the three Microsoft products are all `m365`. A table showing
 * it alone gives an administrator four identical rows, no way to tell which is which,
 * and a Re-sync button whose accessible name is the wrong noun. The provider id from
 * `config_json` is the distinguishing fact; the account label distinguishes two people's
 * connections to the same provider.
 */
function sourceName(source: { system: string; provider?: string | null }): string {
  if (!source.provider) return source.system === "local" ? "Local corpus" : source.system;
  return PROVIDER_LABELS[source.provider] ?? source.provider;
}

/** The registry's display names, mirrored. A provider missing here shows its id. */
const PROVIDER_LABELS: Record<string, string> = {
  google_drive: "Google Drive",
  gmail: "Gmail",
  google_calendar: "Google Calendar",
  google_meet: "Google Meet",
  onedrive: "OneDrive",
  teams: "Microsoft Teams",
  sharepoint: "SharePoint",
  slack: "Slack",
  github: "GitHub",
  jira: "Jira",
  confluence: "Confluence",
  zoom: "Zoom",
};

function SourceStatus({ status }: { status: string }) {
  const tone =
    status === "idle" || status === "ok"
      ? "good"
      : status === "syncing"
        ? "attention"
        : status === "error"
          ? "bad"
          : "neutral";
  return <Pill tone={tone}>{status}</Pill>;
}

export default function SourcesPage() {
  const capabilities = useCapabilities();
  const queryClient = useQueryClient();
  const mayRead = can(capabilities, "integration:read");
  // Watching a stalled source and re-running it are different privileges: an Analyst
  // holds `integration:read` and no button, the IT Admin holds both. The API refuses
  // the request whatever this decides.
  const mayResync = can(capabilities, "integration:connect");

  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: api.sources,
    enabled: mayRead,
  });

  const resync = useMutation({
    mutationFn: (source: { id: string; system: string }) => api.resyncSource(source.id),
    onSuccess: (_queued, source) => {
      toast.success(
        `Re-sync queued for ${sourceName(source)}. The worker walks the source and enqueues one ingestion job per changed document.`,
      );
      // Both surfaces changed: the source's in-flight counter, and the queue itself.
      void queryClient.invalidateQueries({ queryKey: ["sources"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (error: unknown) => toast.error(classifyApiError(error).message),
  });
  const [systemFilter, setSystemFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const items = useMemo(() => sources.data?.items ?? [], [sources.data]);
  const systems = useMemo(
    () => Array.from(new Set(items.map((s) => s.system))).sort(),
    [items],
  );
  const statuses = useMemo(
    () => Array.from(new Set(items.map((s) => s.status))).sort(),
    [items],
  );
  const visible = items.filter(
    (s) =>
      (systemFilter === "all" || s.system === systemFilter) &&
      (statusFilter === "all" || s.status === statusFilter),
  );

  if (!mayRead) {
    return <PermissionDenied what="permission to see knowledge sources" />;
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="Knowledge" title="Knowledge sources">
        Where organisational memory comes from. Content is evaluated against privacy,
        relevance and access policies during ingestion — what appears here is the
        operational state of each source, never its contents.
      </PageHeader>

      {sources.error ? (
        <FailureState
          failure={classifyApiError(sources.error)}
          onRetry={() => void sources.refetch()}
          deniedWhat="reading knowledge sources"
        />
      ) : sources.isPending ? (
        <LoadingRegion label="Loading knowledge sources.">
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        </LoadingRegion>
      ) : items.length === 0 ? (
        <EmptyState title="No knowledge sources yet">
          <p>
            Nothing has been connected. Sources appear here when a connector is configured
            and its first ingestion runs — nothing on this screen is ever estimated.
          </p>
        </EmptyState>
      ) : (
        <>
        <div className="flex flex-wrap gap-3">
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            System
            <select
              value={systemFilter}
              onChange={(event) => setSystemFilter(event.target.value)}
              className="h-9 rounded-lg border border-hairline-strong bg-surface/40 px-2.5 text-xs text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
            >
              <option value="all">All</option>
              {systems.map((system) => (
                <option key={system} value={system}>
                  {system}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            Status
            <select
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
              className="h-9 rounded-lg border border-hairline-strong bg-surface/40 px-2.5 text-xs text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
            >
              <option value="all">All</option>
              {statuses.map((status) => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </select>
          </label>
        </div>
        <TableShell
          caption={
            mayResync
              ? "Knowledge sources with provider, account, sync status, last synchronised time, document count and a re-sync control per source."
              : "Knowledge sources with provider, account, sync status, last synchronised time and document count."
          }
          headings={[
            "Source",
            "Account",
            "Status",
            "Last synchronised",
            "Documents",
            "In flight",
            "Indexed",
            "Failed",
            ...(mayResync ? ["Actions"] : []),
          ]}
          minWidth="min-w-[48rem]"
        >
          {visible.length === 0 ? (
            <tr>
              {/* The page's own EmptyState covers "no sources at all"; this covers
                  the far more common "none match the filters", which used to render
                  a bare header with nothing under it and no way to tell whether the
                  filter was wrong or the data was missing. */}
              <td colSpan={mayResync ? 9 : 8} className="px-5 py-8 text-center text-xs text-muted-foreground">
                No sources match those filters. Clear them to see everything.
              </td>
            </tr>
          ) : null}
          {visible.map((source) => (
            <tr key={source.id} className="border-b border-hairline last:border-b-0">
              <td className="px-5 py-3.5 text-xs text-foreground">
                {sourceName(source)}
              </td>
              <td className="px-5 py-3.5 font-mono text-xs text-muted-foreground">
                {source.account_label ?? "—"}
              </td>
              <td className="px-5 py-3.5">
                <SourceStatus status={source.status} />
              </td>
              <td className="px-5 py-3.5 text-xs text-muted-foreground">
                <When iso={source.last_sync_at} />
              </td>
              <td className="px-5 py-3.5 text-xs tabular-nums text-muted-foreground">
                {source.document_count}
              </td>
              <td className="px-5 py-3.5 text-xs tabular-nums text-muted-foreground">
                {source.jobs_pending}
              </td>
              <td className="px-5 py-3.5 text-xs tabular-nums text-muted-foreground">
                {source.jobs_completed}
              </td>
              <td className="px-5 py-3.5">
                <Pill tone={source.jobs_failed > 0 ? "bad" : "neutral"}>
                  {source.jobs_failed}
                </Pill>
              </td>
              {mayResync ? (
                <td className="px-5 py-3.5">
                  <button
                    type="button"
                    // Every row's button would otherwise be called "Re-sync" in a list
                    // of links and buttons, where the surrounding row is not read out.
                    aria-label={`Re-sync ${sourceName(source)}${
                      source.account_label ? ` for ${source.account_label}` : ""
                    }`}
                    // One mutation for the table, so only the row that was clicked
                    // reads as busy — `variables` is what distinguishes them. Disabling
                    // on `isPending` alone froze every other row's button too, which is
                    // exactly what this comment claimed it did not do; the server holds
                    // one walk per source, so the other rows need no protection.
                    disabled={resync.isPending && resync.variables?.id === source.id}
                    onClick={() => resync.mutate({ id: source.id, system: source.system })}
                    className="rounded-lg border border-hairline-strong px-3 py-1.5 text-xs font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
                  >
                    {resync.isPending && resync.variables?.id === source.id
                      ? "Queueing…"
                      : "Re-sync"}
                  </button>
                </td>
              ) : null}
            </tr>
          ))}
        </TableShell>
        </>
      )}
    </div>
  );
}
