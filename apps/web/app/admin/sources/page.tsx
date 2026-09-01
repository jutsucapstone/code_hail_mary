"use client";

import { useMemo, useState } from "react";

import { useQuery } from "@tanstack/react-query";

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
 * Knowledge sources — what has been connected and what it has produced.
 *
 * Shows sync *state*, never configuration *content*: the API withholds `config_json`
 * because corpus paths and connector settings describe infrastructure nobody needs on a
 * health row. Document counts are current versions only — superseded versions are
 * history, not inventory.
 */

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
  const mayRead = can(capabilities, "integration:read");

  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: api.sources,
    enabled: mayRead,
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
          caption="Knowledge sources with system, sync status, last synchronised time and document count."
          headings={[
            "System",
            "Status",
            "Last synchronised",
            "Documents",
            "In flight",
            "Indexed",
            "Failed",
          ]}
          minWidth="min-w-[48rem]"
        >
          {visible.map((source) => (
            <tr key={source.id} className="border-b border-hairline last:border-b-0">
              <td className="px-5 py-3.5 font-mono text-xs text-foreground">{source.system}</td>
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
            </tr>
          ))}
        </TableShell>
        </>
      )}
    </div>
  );
}
