"use client";

import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { useCapabilities } from "@/components/admin/admin-shell";
import {
  LoadMore,
  PageHeader,
  Pill,
  TableShell,
  When,
} from "@/components/admin/page-scaffold";
import {
  EmptyState,
  FailureState,
  LoadingRegion,
  PermissionDenied,
  Skeleton,
} from "@/components/states";
import { api, type AuditEntry } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import { can } from "@/lib/permissions";

/**
 * The audit trail — every security-sensitive action, immutably recorded.
 *
 * Renders exactly what `GET /v1/audit` returns: opaque actor ids resolved to JUTSU IDs
 * where possible, actions, outcomes and timestamps. Deliberately **no email addresses**
 * anywhere on this screen — the API does not return them (§4.9) and the UI must not go
 * looking for them through another endpoint to "enrich" a row.
 *
 * Filtering is server-side. The trail can be arbitrarily long, and a client-side filter
 * over one fetched page would silently answer "no denials" while denials sat on page
 * two.
 */

const OUTCOMES = ["success", "denied", "failure"] as const;

function OutcomePill({ outcome }: { outcome: string }) {
  const tone = outcome === "success" ? "good" : outcome === "denied" ? "attention" : "bad";
  return <Pill tone={tone}>{outcome}</Pill>;
}

export default function AuditPage() {
  const capabilities = useCapabilities();
  const [outcome, setOutcome] = useState<string>("");
  // Older pages accumulate under the newest-first head page as the reader walks back.
  const [older, setOlder] = useState<AuditEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const mayRead = can(capabilities, "audit:read");

  const head = useQuery({
    queryKey: ["audit", { outcome: outcome || null }],
    queryFn: () => api.audit({ outcome: outcome || null }),
    enabled: mayRead,
    // Keep the previous page on screen while a filter change refetches; a flash of
    // skeleton on every filter click reads as the page being broken.
    placeholderData: keepPreviousData,
  });

  if (!mayRead) {
    return <PermissionDenied what="permission to read the audit trail" />;
  }

  async function loadOlder() {
    const next = cursor ?? head.data?.next_cursor;
    if (!next) return;
    setLoadingMore(true);
    try {
      const page = await api.audit({ cursor: next, outcome: outcome || null });
      setOlder((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
    } finally {
      setLoadingMore(false);
    }
  }

  function changeOutcome(value: string) {
    setOutcome(value);
    // A filter change starts a fresh walk; stale older pages belong to the old filter.
    setOlder([]);
    setCursor(null);
  }

  const rows = [...(head.data?.items ?? []), ...older];
  const more = cursor ?? head.data?.next_cursor;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="Operations" title="Audit log">
        Every security-sensitive action, immutably recorded — the application role cannot
        update or delete these rows. Actors appear as JUTSU IDs, never as email addresses.
      </PageHeader>

      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by outcome">
        {["", ...OUTCOMES].map((value) => (
          <button
            key={value || "all"}
            type="button"
            onClick={() => changeOutcome(value)}
            aria-pressed={outcome === value}
            className={`rounded-lg border px-3 py-1.5 text-sm transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand ${
              outcome === value
                ? "border-brand/40 bg-brand/8 text-foreground"
                : "border-hairline-strong text-muted-foreground hover:text-foreground"
            }`}
          >
            {value || "All outcomes"}
          </button>
        ))}
      </div>

      {head.error ? (
        <FailureState
          failure={classifyApiError(head.error)}
          onRetry={() => void head.refetch()}
          deniedWhat="reading the audit trail"
        />
      ) : head.isPending ? (
        <LoadingRegion label="Loading the audit trail.">
          <div className="flex flex-col gap-2">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        </LoadingRegion>
      ) : rows.length === 0 ? (
        <EmptyState title="Nothing recorded yet">
          <p>
            {outcome
              ? `No ${outcome} events in the trail. Clearing the filter shows everything.`
              : "Security-sensitive actions — invitations, role changes, identity links — appear here as they happen."}
          </p>
        </EmptyState>
      ) : (
        <>
          <TableShell
            caption="Audit events, newest first, with actor, action, resource and outcome."
            headings={["When", "Actor", "Action", "Resource", "Outcome"]}
          >
            {rows.map((entry) => (
              <tr key={entry.id} className="border-b border-hairline last:border-b-0">
                <td className="px-5 py-3.5 text-xs text-muted-foreground">
                  <When iso={entry.ts} />
                </td>
                <td className="px-5 py-3.5 font-mono text-xs text-muted-foreground">
                  {entry.actor_jutsu_id ?? entry.actor_type}
                </td>
                <td className="px-5 py-3.5 font-mono text-xs text-foreground">{entry.action}</td>
                <td className="px-5 py-3.5 text-xs text-muted-foreground">
                  {entry.resource_type}
                </td>
                <td className="px-5 py-3.5">
                  <OutcomePill outcome={entry.outcome} />
                </td>
              </tr>
            ))}
          </TableShell>
          {more ? <LoadMore onClick={() => void loadOlder()} pending={loadingMore} /> : null}
        </>
      )}
    </div>
  );
}
