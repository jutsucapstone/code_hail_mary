"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, Database, Waypoints } from "lucide-react";

import { useCapabilities } from "@/components/admin/admin-shell";
import { PageHeader, Pill, StatStrip } from "@/components/admin/page-scaffold";
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
 * System health — what is actually reachable, per the API's own probes.
 *
 * `/readyz` is public infrastructure (the platform polls it with no session), but this
 * page still gates on `org:read` like the rest of Operations: the raw endpoint saying
 * "degraded" to a load balancer is one thing, a console page inviting every member to
 * watch dependency state is another.
 *
 * Three states per dependency, honestly: `ok` means a probe connected and answered,
 * `failed` means it did not, and `not_configured` means this deployment does not use
 * the dependency — which is a statement, not an outage.
 */

interface Readiness {
  status: string;
  checks: Record<string, string>;
}

function checkTone(value: string): "good" | "bad" | "neutral" {
  return value === "ok" ? "good" : value === "failed" ? "bad" : "neutral";
}

export default function HealthPage() {
  const capabilities = useCapabilities();
  const mayRead = can(capabilities, "org:read");

  const readiness = useQuery({
    queryKey: ["readyz"],
    queryFn: () => api.ready(),
    enabled: mayRead,
    // Health should not linger: poll while the page is open.
    refetchInterval: 30_000,
  });
  const jobs = useQuery({
    queryKey: ["jobs", "stats"],
    queryFn: api.jobStats,
    enabled: mayRead,
    refetchInterval: 30_000,
  });

  if (!mayRead) {
    return <PermissionDenied what="permission to see system health" />;
  }

  const ready = readiness.data as Readiness | undefined;

  return (
    <div className="flex flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="Operations" title="System health">
        Live dependency probes and queue pressure, refreshed every thirty seconds while
        this page is open. &ldquo;ok&rdquo; means a probe connected and answered — never
        that a URL merely exists in configuration.
      </PageHeader>

      {readiness.error ? (
        <FailureState
          failure={classifyApiError(readiness.error)}
          onRetry={() => void readiness.refetch()}
          deniedWhat="reading system health"
        />
      ) : !ready ? (
        <LoadingRegion label="Probing dependencies.">
          <Skeleton className="h-32" />
        </LoadingRegion>
      ) : (
        <>
          <section
            aria-labelledby="deps-heading"
            className="rounded-2xl border border-hairline bg-surface/40 p-6"
          >
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 id="deps-heading" className="display text-lg font-semibold">
                Dependencies
              </h2>
              <Pill tone={ready.status === "ready" ? "good" : "bad"}>{ready.status}</Pill>
            </div>
            <ul className="mt-5 flex flex-col gap-3">
              {Object.entries(ready.checks).map(([name, value]) => (
                <li
                  key={name}
                  className="flex items-center justify-between gap-4 rounded-xl border border-hairline bg-background px-4 py-3"
                >
                  <span className="flex items-center gap-3">
                    <span
                      aria-hidden="true"
                      className="flex size-8 items-center justify-center rounded-lg border border-hairline-strong bg-surface text-brand"
                    >
                      {name === "postgres" ? (
                        <Database className="size-4" />
                      ) : name === "neo4j" ? (
                        <Waypoints className="size-4" />
                      ) : (
                        <Activity className="size-4" />
                      )}
                    </span>
                    <span className="font-mono text-sm text-foreground">{name}</span>
                  </span>
                  <Pill tone={checkTone(value)}>{value.replace("_", " ")}</Pill>
                </li>
              ))}
            </ul>
          </section>

          {jobs.error ? (
            <section aria-labelledby="queue-heading" className="flex flex-col gap-4">
              <h2 id="queue-heading" className="display text-xl font-semibold">
                Queue pressure
              </h2>
              {/* On a health page especially, a silently missing section reads as
                  "queue fine" — the one page whose job is to say what is not. */}
              <FailureState
                failure={classifyApiError(jobs.error)}
                onRetry={() => void jobs.refetch()}
                deniedWhat="reading queue statistics"
              />
            </section>
          ) : jobs.data ? (
            <section aria-labelledby="queue-heading" className="flex flex-col gap-4">
              <h2 id="queue-heading" className="display text-xl font-semibold">
                Queue pressure
              </h2>
              <StatStrip
                columns={3}
                stats={[
                  {
                    id: "pending",
                    label: "Jobs waiting",
                    value: jobs.data.by_state["pending"] ?? 0,
                  },
                  { id: "failed", label: "Failed in the last 24h", value: jobs.data.failed_24h },
                  { id: "dead", label: "Dead-lettered", value: jobs.data.dead_letter },
                ]}
              />
            </section>
          ) : null}
        </>
      )}
    </div>
  );
}
