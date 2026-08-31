"use client";

import { useCallback, useEffect, useState } from "react";

import { ErrorState, LoadingRegion, Skeleton } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { ApiError, api, type SourceIdentity } from "@/lib/api";

/**
 * The accounts you are known by, and what they let you read.
 *
 * Read-only, and that is not a gap. `GET /v1/me/identities` exists; there is no endpoint
 * for a person to link their own — deliberately, because linking a subject to yourself is
 * exactly the escalation the API refuses even for an Owner. Somebody else grants you an
 * identity, or you do not have it.
 *
 * This page answers the question a member actually asks when a search comes back empty:
 * *what am I allowed to see?* Before it existed, the honest answer lived only in a
 * database table.
 */
export default function MyIdentitiesPage() {
  const [identities, setIdentities] = useState<SourceIdentity[] | null>(null);
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(null);

  // Nothing set synchronously — see the note in `app/admin/identities/page.tsx`.
  const load = useCallback(() => {
    api
      .myIdentities()
      .then((page) => {
        setIdentities(page.items);
        setFailure(null);
      })
      .catch((error: unknown) => {
        setIdentities([]);
        setFailure({
          message: error instanceof ApiError ? error.message : "That did not load.",
          requestId: error instanceof ApiError ? error.requestId : undefined,
        });
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const active = identities?.filter((identity) => identity.is_active) ?? [];

  return (
    <div>
      <h1 className="display text-2xl font-semibold sm:text-3xl">Source identities</h1>
      <p className="mt-3 max-w-prose text-pretty text-base leading-relaxed text-muted-foreground">
        JUTSU grants access to documents through the provider accounts you are known by,
        not through your login. These are yours. If a search returns nothing, this is
        usually why.
      </p>

      {identities === null ? (
        <LoadingRegion label="Loading your identities">
          <div className="mt-8 space-y-2">
            {[0, 1].map((n) => (
              <Skeleton key={n} className="h-14 w-full" />
            ))}
          </div>
        </LoadingRegion>
      ) : failure ? (
        <div className="mt-8">
          <ErrorState
            message={failure.message}
            requestId={failure.requestId}
            onRetry={load}
          />
        </div>
      ) : active.length === 0 ? (
        <div className="mt-8 rounded-2xl border border-hairline bg-surface/40 p-6">
          <p className="eyebrow text-muted-foreground/80">No active identities</p>
          <p className="mt-3 text-pretty text-sm leading-relaxed text-muted-foreground">
            You are not currently known by any provider account, so no document grants
            match you and searches will return nothing. An administrator can link one for
            you — you cannot link your own.
          </p>
        </div>
      ) : (
        <ul className="mt-8 space-y-3">
          {active.map((identity) => (
            <li
              key={identity.id}
              className="flex items-center justify-between gap-4 rounded-2xl border border-hairline bg-surface/40 p-5"
            >
              <div className="min-w-0">
                {/* The assembled principal, because that is the exact string a
                    document grant is matched against. */}
                <p className="truncate font-mono text-sm">
                  {identity.source_system}:{identity.subject}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  Linked{" "}
                  {new Date(identity.linked_at).toLocaleDateString(undefined, {
                    year: "numeric",
                    month: "short",
                    day: "numeric",
                  })}
                </p>
              </div>
              <Badge variant="default">Active</Badge>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
