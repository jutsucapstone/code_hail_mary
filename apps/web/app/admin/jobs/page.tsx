"use client";

import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { AlertOctagon, ListTodo, Loader2, Skull } from "lucide-react";

import { useCapabilities } from "@/components/admin/admin-shell";
import {
  LoadMore,
  PageHeader,
  Pill,
  StatStrip,
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
import { api, type JobPage } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import { can } from "@/lib/permissions";

/**
 * Jobs & sync — the ingestion queue's state, without its contents.
 *
 * Rows carry the classified `failure_kind` and never the exception text: the API
 * withholds it deliberately, because error strings can embed file paths and provider
 * payloads (§4.9). What an operator needs — which job, what state, how many attempts,
 * what class of failure — is all here.
 */

const STATES = ["pending", "running", "completed", "failed", "dead_letter"] as const;

function StatePill({ state }: { state: string }) {
  const tone =
    state === "completed"
      ? "good"
      : state === "failed" || state === "dead_letter"
        ? "bad"
        : state === "running"
          ? "attention"
          : "neutral";
  return <Pill tone={tone}>{state}</Pill>;
}

export default function JobsPage() {
  const capabilities = useCapabilities();
  const [state, setState] = useState<string>("");
  const [older, setOlder] = useState<JobPage["items"]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const mayRead = can(capabilities, "org:read");

  const stats = useQuery({
    queryKey: ["jobs", "stats"],
    queryFn: api.jobStats,
    enabled: mayRead,
  });
  const head = useQuery({
    queryKey: ["jobs", { state: state || null }],
    queryFn: () => api.jobs({ state: state || null }),
    enabled: mayRead,
    placeholderData: keepPreviousData,
  });

  if (!mayRead) {
    return <PermissionDenied what="permission to see this organisation's jobs" />;
  }

  async function loadOlder() {
    const next = cursor ?? head.data?.next_cursor;
    if (!next) return;
    setLoadingMore(true);
    try {
      const page = await api.jobs({ cursor: next, state: state || null });
      setOlder((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
    } finally {
      setLoadingMore(false);
    }
  }

  function changeState(value: string) {
    setState(value);
    setOlder([]);
    setCursor(null);
  }

  const rows = [...(head.data?.items ?? []), ...older];
  const more = cursor ?? head.data?.next_cursor;
  const byState = stats.data?.by_state ?? {};

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="Operations" title="Jobs & sync">
        Ingestion and embedding runs, their state, and what failed. Failures are shown by
        class — the raw error text stays in the worker&apos;s logs, where paths and
        provider payloads belong.
      </PageHeader>

      {stats.data ? (
        <StatStrip
          columns={4}
          stats={[
            { id: "pending", label: "Pending", value: byState["pending"] ?? 0, icon: ListTodo },
            { id: "running", label: "Running", value: byState["running"] ?? 0, icon: Loader2 },
            {
              id: "failed",
              label: "Failed in the last 24h",
              value: stats.data.failed_24h,
              icon: AlertOctagon,
            },
            { id: "dead", label: "Dead-lettered", value: stats.data.dead_letter, icon: Skull },
          ]}
        />
      ) : null}

      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by state">
        {["", ...STATES].map((value) => (
          <button
            key={value || "all"}
            type="button"
            onClick={() => changeState(value)}
            aria-pressed={state === value}
            className={`rounded-lg border px-3 py-1.5 text-sm transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand ${
              state === value
                ? "border-brand/40 bg-brand/8 text-foreground"
                : "border-hairline-strong text-muted-foreground hover:text-foreground"
            }`}
          >
            {value ? value.replace("_", " ") : "All states"}
          </button>
        ))}
      </div>

      {head.error ? (
        <FailureState
          failure={classifyApiError(head.error)}
          onRetry={() => void head.refetch()}
          deniedWhat="reading the job queue"
        />
      ) : head.isPending ? (
        <LoadingRegion label="Loading jobs.">
          <div className="flex flex-col gap-2">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        </LoadingRegion>
      ) : rows.length === 0 ? (
        <EmptyState title="No jobs here">
          <p>
            {state
              ? `Nothing is ${state.replace("_", " ")} right now.`
              : "Nothing has been queued yet. Jobs appear when a knowledge source is ingested."}
          </p>
        </EmptyState>
      ) : (
        <>
          <TableShell
            caption="Jobs, most recently updated first, with kind, state, attempts and failure class."
            headings={["Updated", "Kind", "State", "Attempts", "Failure class"]}
          >
            {rows.map((job) => (
              <tr key={job.id} className="border-b border-hairline last:border-b-0">
                <td className="px-5 py-3.5 text-xs text-muted-foreground">
                  <When iso={job.updated_at} />
                </td>
                <td className="px-5 py-3.5 font-mono text-xs text-foreground">{job.kind}</td>
                <td className="px-5 py-3.5">
                  <StatePill state={job.state} />
                </td>
                <td className="px-5 py-3.5 text-xs tabular-nums text-muted-foreground">
                  {job.attempts}
                </td>
                <td className="px-5 py-3.5 font-mono text-xs text-muted-foreground">
                  {job.failure_kind ?? "—"}
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
