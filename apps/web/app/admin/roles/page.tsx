"use client";

import { useQuery } from "@tanstack/react-query";

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
 * Roles & permissions — who can do what, and why.
 *
 * The catalogue is fetched from the API, which reads it from the database — the runtime
 * authority that migration 0002 seeded and then made read-only to the application role.
 * Nothing on this page is a hand-maintained copy, so a catalogue change lands here
 * without anyone editing the frontend.
 *
 * Role *changes* happen on the Employees page, next to the person they affect. This
 * page is the map: what each role means before you hand it to somebody.
 */

export default function RolesPage() {
  const capabilities = useCapabilities();
  const mayRead = can(capabilities, "org:read");
  const mayAssign = can(capabilities, "member:assign_role");

  const catalogue = useQuery({
    queryKey: ["roles"],
    queryFn: api.roles,
    enabled: mayRead,
    // The catalogue changes by migration, not at runtime; an hour of staleness is fine.
    staleTime: 60 * 60 * 1000,
  });

  if (!mayRead) {
    return <PermissionDenied what="permission to see the role catalogue" />;
  }

  return (
    <div className="flex flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="Access" title="Roles & permissions">
        {mayAssign
          ? "What each role may do. To change someone's role, use the control beside their name in Employees — changes are audited with both the old and new role."
          : "What each role may do. Changing a role needs the member:assign_role permission, which your role does not include."}
      </PageHeader>

      {catalogue.error ? (
        <FailureState
          failure={classifyApiError(catalogue.error)}
          onRetry={() => void catalogue.refetch()}
          deniedWhat="reading the role catalogue"
        />
      ) : catalogue.isPending ? (
        <LoadingRegion label="Loading the role catalogue.">
          <div className="flex flex-col gap-3">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-28" />
            ))}
          </div>
        </LoadingRegion>
      ) : (
        <div className="flex flex-col gap-4">
          {catalogue.data.roles.map((role) => (
            <section
              key={role.key}
              aria-labelledby={`role-${role.key}`}
              className="rounded-2xl border border-hairline bg-surface/40 p-6 [@media(max-height:820px)]:p-4"
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 id={`role-${role.key}`} className="display text-lg font-semibold">
                  {role.label}
                </h2>
                <span className="font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground">
                  rank {role.rank}
                  {capabilities.role === role.key ? " · your role" : ""}
                </span>
              </div>
              {/* The permission strings themselves, not a prose paraphrase: these are
                  the exact values routes declare, so an administrator reading a 403 can
                  match what they see here against what the API said. */}
              <ul className="mt-4 flex flex-wrap gap-1.5" aria-label={`${role.label} permissions`}>
                {role.permissions.map((permission) => (
                  <li key={permission}>
                    <Pill tone={permission.endsWith(":read") || permission.startsWith("profile") ? "neutral" : "attention"}>
                      {permission}
                    </Pill>
                  </li>
                ))}
              </ul>
            </section>
          ))}

          <p className="max-w-prose text-pretty text-xs leading-relaxed text-muted-foreground">
            No permission grants access to any document. What a person can read is decided
            by their linked source identities against each document&apos;s access list —
            an Owner with no linked identity sees no evidence at all.
          </p>
        </div>
      )}
    </div>
  );
}
