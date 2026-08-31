"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { useCapabilities } from "@/components/admin/admin-shell";
import { PageHeader, Pill } from "@/components/admin/page-scaffold";
import {
  FailureState,
  LoadingRegion,
  PermissionDenied,
  Skeleton,
} from "@/components/states";
import { api } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import { can } from "@/lib/permissions";

/**
 * Connection policies — which applications people may connect.
 *
 * Absence of a stored row means allowed; that is the product default, and the page says
 * so rather than presenting thirteen mysterious toggles. Restricting a provider stops
 * NEW connections; it deliberately does not sever existing ones — a toggle must not be
 * a silent mass revocation. Existing connections stay visible under Organisation apps,
 * and revoking one is an explicit per-person act with its own audit row.
 */
export default function ConnectionPoliciesPage() {
  const capabilities = useCapabilities();
  const queryClient = useQueryClient();

  const mayRead = can(capabilities, "integration:read");
  const mayWrite = can(capabilities, "org:update");

  const policies = useQuery({
    queryKey: ["connection-policies"],
    queryFn: api.connectionPolicies,
    enabled: mayRead,
  });

  const setPolicy = useMutation({
    mutationFn: ({ provider, allowed }: { provider: string; allowed: boolean }) =>
      api.setConnectionPolicy(provider, allowed),
    onSuccess: (row) => {
      toast.success(
        row.allowed
          ? `${row.name} can now be connected.`
          : `${row.name} restricted. Existing connections stay until revoked individually.`,
      );
      void queryClient.invalidateQueries({ queryKey: ["connection-policies"] });
      void queryClient.invalidateQueries({ queryKey: ["integrations"] });
    },
    onError: (error: unknown) => toast.error(classifyApiError(error).message),
  });

  if (!mayRead) {
    return <PermissionDenied what="permission to see connection policies" />;
  }

  return (
    <div className="flex flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="Integrations" title="Connection policies">
        Which applications people in this organisation may connect. Everything is
        allowed until restricted here. Restricting stops new connections — existing ones
        remain until revoked individually, where the audit trail can name what happened.
      </PageHeader>

      {policies.error ? (
        <FailureState
          failure={classifyApiError(policies.error)}
          onRetry={() => void policies.refetch()}
          deniedWhat="reading connection policies"
        />
      ) : policies.isPending ? (
        <LoadingRegion label="Loading connection policies.">
          <div className="flex flex-col gap-2">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-14" />
            ))}
          </div>
        </LoadingRegion>
      ) : (
        <ul className="flex flex-col gap-2">
          {policies.data.items.map((policy) => (
            <li
              key={policy.provider}
              className="flex items-center justify-between gap-4 rounded-xl border border-hairline bg-surface/40 px-5 py-3.5"
            >
              <div className="flex items-center gap-3">
                <span className="text-sm text-foreground">{policy.name}</span>
                <Pill tone={policy.allowed ? "good" : "bad"}>
                  {policy.allowed ? "allowed" : "restricted"}
                </Pill>
              </div>
              {mayWrite ? (
                <button
                  type="button"
                  disabled={setPolicy.isPending}
                  onClick={() =>
                    setPolicy.mutate({ provider: policy.provider, allowed: !policy.allowed })
                  }
                  className="rounded-lg border border-hairline-strong px-3 py-1.5 text-xs font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
                >
                  {policy.allowed ? "Restrict" : "Allow"}
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
