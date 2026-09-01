"use client";

import { useQuery } from "@tanstack/react-query";

import { useCapabilities } from "@/components/admin/admin-shell";
import { PageHeader, Pill } from "@/components/admin/page-scaffold";
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
 * Departments — as people have declared them, honestly labelled as such.
 *
 * The department field is self-service free text on each person's profile, not a
 * managed entity: two spellings are two rows, and the page says so instead of
 * pretending to a taxonomy nobody curates. The unassigned count is first-class so the
 * numbers add up. Making departments a real table — create, rename, assign, RLS — is
 * its own migration when the organisation model needs it.
 */
export default function DepartmentsPage() {
  const capabilities = useCapabilities();
  const mayRead = can(capabilities, "member:read");

  const departments = useQuery({
    queryKey: ["departments"],
    queryFn: api.departments,
    enabled: mayRead,
  });

  if (!mayRead) {
    return <PermissionDenied what="permission to see departments" />;
  }

  return (
    <div className="flex flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="People" title="Departments">
        Teams as people have recorded them on their own profiles. Free text, not a
        managed list — identical names group together, different spellings do not.
      </PageHeader>

      {departments.error ? (
        <FailureState
          failure={classifyApiError(departments.error)}
          onRetry={() => void departments.refetch()}
          deniedWhat="reading departments"
        />
      ) : departments.isPending ? (
        <LoadingRegion label="Loading departments.">
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        </LoadingRegion>
      ) : departments.data.items.length === 0 ? (
        <EmptyState title="No departments recorded yet">
          <p>
            Departments appear as people fill in the team field on their profiles.
            {departments.data.unassigned > 0
              ? ` ${departments.data.unassigned} ${
                  departments.data.unassigned === 1 ? "person has" : "people have"
                } no department recorded.`
              : ""}
          </p>
        </EmptyState>
      ) : (
        <>
          <ul className="flex flex-col gap-2">
            {departments.data.items.map((row) => (
              <li
                key={row.name}
                className="flex items-center justify-between gap-4 rounded-xl border border-hairline bg-surface/40 px-5 py-3.5"
              >
                <span className="text-sm text-foreground">{row.name}</span>
                <Pill tone="neutral">
                  {row.members} {row.members === 1 ? "person" : "people"}
                </Pill>
              </li>
            ))}
          </ul>
          {departments.data.unassigned > 0 ? (
            <p className="text-xs text-muted-foreground">
              {departments.data.unassigned}{" "}
              {departments.data.unassigned === 1 ? "person has" : "people have"} no
              department recorded on their profile.
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}
