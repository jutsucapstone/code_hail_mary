"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Pill, When } from "@/components/admin/page-scaffold";
import { EmptyState, FailureState, LoadingRegion, Skeleton } from "@/components/states";
import { api } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * One employee's connections, for the governance detail (§3).
 *
 * Operational metadata only: provider, state, account label, last sync, measured
 * document count. No content, no credentials — the API withholds both by construction.
 * Revocation is the administrative act; the server rank-checks it (an equal rank is
 * refused there, not here), and the connection's owner keeps their source identity —
 * revoking a *pipe* is not revoking *who they are* (ADR 0014).
 */

const STATUS_TONE: Record<string, "good" | "attention" | "bad" | "neutral"> = {
  connected: "good",
  syncing: "attention",
  connecting: "attention",
  error: "bad",
  reauth_required: "bad",
};

export function EmployeeConnections({
  userId,
  mayRevoke,
}: {
  userId: string;
  mayRevoke: boolean;
}) {
  const queryClient = useQueryClient();
  const [revoking, setRevoking] = useState<string | null>(null);

  const connections = useQuery({
    queryKey: ["employee-connections", userId],
    queryFn: () => api.employeeConnections(userId),
  });

  async function revoke(connectionId: string, provider: string) {
    setRevoking(connectionId);
    try {
      await api.revokeConnection(connectionId);
      toast.success(`${provider} connection revoked.`);
      await queryClient.invalidateQueries({ queryKey: ["employee-connections", userId] });
    } catch (error) {
      toast.error(classifyApiError(error).message);
    } finally {
      setRevoking(null);
    }
  }

  if (connections.error) {
    return (
      <FailureState
        failure={classifyApiError(connections.error)}
        onRetry={() => void connections.refetch()}
        deniedWhat="reading this person's connections"
      />
    );
  }
  if (connections.isPending) {
    return (
      <LoadingRegion label="Loading connections.">
        <Skeleton className="h-10" />
      </LoadingRegion>
    );
  }
  if (connections.data.items.length === 0) {
    return (
      <EmptyState title="No connected applications">
        <p>This person has not connected any work tools.</p>
      </EmptyState>
    );
  }

  return (
    <ul className="flex flex-col gap-2">
      {connections.data.items.map((connection) => (
        <li
          key={connection.id}
          className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-hairline bg-surface/40 px-4 py-3"
        >
          <div className="flex min-w-0 flex-wrap items-center gap-3 text-xs">
            <span className="font-mono uppercase tracking-[0.12em] text-foreground">
              {connection.provider}
            </span>
            <Pill tone={STATUS_TONE[connection.status] ?? "neutral"}>
              {connection.status.replaceAll("_", " ")}
            </Pill>
            <span className="truncate text-muted-foreground">
              {connection.account_label ?? "—"}
            </span>
            <span className="text-muted-foreground">
              {connection.last_sync_at ? (
                <>
                  Last sync <When iso={connection.last_sync_at} />
                </>
              ) : (
                "Never synced"
              )}
            </span>
            <span className="tabular-nums text-muted-foreground">
              {connection.document_count} documents
            </span>
          </div>
          {mayRevoke ? (
            <button
              type="button"
              disabled={revoking === connection.id}
              onClick={() => void revoke(connection.id, connection.provider)}
              className="rounded-lg border border-hairline-strong px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:border-destructive/40 hover:text-destructive focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
            >
              {revoking === connection.id ? "Revoking…" : "Revoke"}
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
