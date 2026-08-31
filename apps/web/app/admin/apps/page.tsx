"use client";

import { useQuery } from "@tanstack/react-query";

import { useCapabilities } from "@/components/admin/admin-shell";
import { PageHeader, Pill, TableShell } from "@/components/admin/page-scaffold";
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
 * Organisation apps — connection governance, as counts.
 *
 * Deliberately aggregate: "12 connected, 1 requires re-authentication" is what
 * governance needs, and the endpoint returns exactly that. No account identities appear
 * here — an administrator who needs one person's detail reaches it through that person,
 * where the act is specific and attributable, not through a browsable roster.
 */

const HEALTHY = new Set(["connected"]);
const ATTENTION = new Set(["error", "reauth_required"]);

export default function OrganisationAppsPage() {
  const capabilities = useCapabilities();
  const mayRead = can(capabilities, "integration:read");

  const summary = useQuery({
    queryKey: ["connections", "summary"],
    queryFn: api.connectionSummary,
    enabled: mayRead,
  });

  if (!mayRead) {
    return <PermissionDenied what="permission to see organisation integrations" />;
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="Integrations" title="Organisation apps">
        Which applications people have connected, and whether those connections are
        healthy. Employees connect their own applications; this page governs and
        monitors, it does not connect on anyone&apos;s behalf.
      </PageHeader>

      {summary.error ? (
        <FailureState
          failure={classifyApiError(summary.error)}
          onRetry={() => void summary.refetch()}
          deniedWhat="reading connection state"
        />
      ) : summary.isPending ? (
        <LoadingRegion label="Loading connection state.">
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        </LoadingRegion>
      ) : summary.data.items.length === 0 ? (
        <EmptyState title="Nothing connected yet">
          <p>
            When people connect their applications from My Integrations, each provider
            appears here with its connection counts and health.
          </p>
        </EmptyState>
      ) : (
        <TableShell
          caption="Connected applications with totals and health per provider."
          headings={["Application", "Connections", "Healthy", "Needs attention"]}
          minWidth="min-w-[34rem]"
        >
          {summary.data.items.map((item) => {
            const healthy = Object.entries(item.by_status)
              .filter(([status]) => HEALTHY.has(status))
              .reduce((n, [, count]) => n + count, 0);
            const attention = Object.entries(item.by_status)
              .filter(([status]) => ATTENTION.has(status))
              .reduce((n, [, count]) => n + count, 0);
            return (
              <tr key={item.provider} className="border-b border-hairline last:border-b-0">
                <td className="px-5 py-3.5 text-foreground">{item.name}</td>
                <td className="px-5 py-3.5 text-xs tabular-nums text-muted-foreground">
                  {item.total}
                </td>
                <td className="px-5 py-3.5">
                  <Pill tone="good">{healthy}</Pill>
                </td>
                <td className="px-5 py-3.5">
                  <Pill tone={attention > 0 ? "bad" : "neutral"}>{attention}</Pill>
                </td>
              </tr>
            );
          })}
        </TableShell>
      )}
    </div>
  );
}
