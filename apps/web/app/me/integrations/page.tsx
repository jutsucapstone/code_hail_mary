"use client";

import { useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Pill, When } from "@/components/admin/page-scaffold";
import {
  FailureState,
  LoadingRegion,
  Skeleton,
} from "@/components/states";
import { api, type IntegrationEntry } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * My Integrations — the employee's own connections, and nobody else's.
 *
 * The whole page is one call to `GET /v1/integrations`: the catalogue, the
 * organisation's policy and the caller's own connection state arrive merged, so what
 * renders cannot disagree with what the backend would enforce.
 *
 * Three kinds of "cannot connect", each rendered as what it is:
 *   - `allowed: false`  — the organisation restricted it. Policy, named as policy.
 *   - `configured: false` — this deployment holds no client credentials for it. Named
 *     as deployment state, never faked with a dead Connect button.
 *   - an error from Connect — the API's own sentence, surfaced verbatim.
 *
 * Connect NAVIGATES to the provider's authorize URL. Nothing OAuth-shaped happens in
 * this page beyond following the URL the backend minted — the state parameter, the
 * token exchange and the credential storage are all server-side.
 */

const STATUS_TONE: Record<string, "good" | "attention" | "bad" | "neutral"> = {
  connected: "good",
  syncing: "attention",
  connecting: "attention",
  error: "bad",
  reauth_required: "bad",
};

function StatusPill({ status }: { status: string }) {
  return <Pill tone={STATUS_TONE[status] ?? "neutral"}>{status.replace("_", " ")}</Pill>;
}

function ConnectorCard({ entry }: { entry: IntegrationEntry }) {
  const queryClient = useQueryClient();
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["integrations"] });

  const connect = useMutation({
    mutationFn: () => api.connect(entry.id),
    onSuccess: (started) => {
      // The provider takes it from here. This is a navigation, not a fetch: the
      // authorize page is theirs, and the browser must carry the person to it.
      window.location.assign(started.authorize_url);
    },
    onError: (error: unknown) => toast.error(classifyApiError(error).message),
  });

  const disconnect = useMutation({
    mutationFn: () => api.disconnectIntegration(entry.connection!.id),
    onSuccess: () => {
      toast.success(`${entry.name} disconnected. Its stored credential has been deleted.`);
      invalidate();
    },
    onError: (error: unknown) => toast.error(classifyApiError(error).message),
  });

  const sync = useMutation({
    mutationFn: () => api.syncNow(entry.connection!.id),
    onSuccess: () => {
      toast.success(`Sync queued for ${entry.name}. Progress appears under its status.`);
      invalidate();
    },
    onError: (error: unknown) => toast.error(classifyApiError(error).message),
  });

  const connection = entry.connection;
  const busy = connect.isPending || disconnect.isPending || sync.isPending;

  return (
    <li className="flex flex-col gap-4 rounded-2xl border border-hairline bg-surface/40 p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="display text-base font-semibold">{entry.name}</h3>
          <p className="mt-1 text-pretty text-xs leading-relaxed text-muted-foreground">
            {entry.description}
          </p>
        </div>
        {connection ? <StatusPill status={connection.status} /> : null}
      </div>

      {connection ? (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
          <dt className="text-muted-foreground">Connected account</dt>
          <dd className="truncate text-foreground">{connection.account_label ?? "—"}</dd>
          <dt className="text-muted-foreground">Last synchronised</dt>
          <dd className="text-muted-foreground">
            {connection.last_sync_at ? <When iso={connection.last_sync_at} /> : "Not yet"}
          </dd>
        </dl>
      ) : null}

      <div className="mt-auto flex flex-wrap gap-2">
        {!connection ? (
          !entry.allowed ? (
            <p className="text-xs text-muted-foreground">
              Your organisation has restricted this application.
            </p>
          ) : !entry.configured ? (
            <p className="text-xs text-muted-foreground">
              Not configured for this deployment yet — an administrator must add its
              credentials before anyone can connect.
            </p>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => connect.mutate()}
              className="rounded-lg bg-brand px-3.5 py-2 text-sm font-medium text-brand-foreground transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
            >
              {connect.isPending ? "Starting…" : `Connect ${entry.name}`}
            </button>
          )
        ) : (
          <>
            {(connection.status === "error" || connection.status === "reauth_required") &&
            entry.configured &&
            entry.allowed ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => connect.mutate()}
                className="rounded-lg bg-brand px-3.5 py-2 text-sm font-medium text-brand-foreground transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
              >
                Reconnect
              </button>
            ) : null}
            {connection.status === "connected" && entry.configured ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => sync.mutate()}
                className="rounded-lg border border-hairline-strong px-3.5 py-2 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
              >
                {sync.isPending ? "Queueing…" : "Sync now"}
              </button>
            ) : null}
            <button
              type="button"
              disabled={busy}
              onClick={() => disconnect.mutate()}
              className="rounded-lg border border-hairline-strong px-3.5 py-2 text-sm font-medium text-muted-foreground transition-colors hover:border-destructive/40 hover:text-destructive focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
            >
              {disconnect.isPending ? "Disconnecting…" : "Disconnect"}
            </button>
          </>
        )}
      </div>
    </li>
  );
}

export default function IntegrationsPage() {
  const searchParams = useSearchParams();
  const connectedParam = searchParams.get("connected");

  const catalogue = useQuery({
    queryKey: ["integrations"],
    queryFn: api.integrations,
  });

  useEffect(() => {
    // The OAuth callback redirects back here with ?connected=<provider>. Announce it
    // once; the catalogue itself already shows the new state.
    if (connectedParam) {
      toast.success(`Connected. JUTSU can now see what ${connectedParam} lets your account see.`);
      window.history.replaceState(null, "", "/me/integrations");
    }
  }, [connectedParam]);

  const groups = new Map<string, IntegrationEntry[]>();
  for (const entry of catalogue.data?.items ?? []) {
    const list = groups.get(entry.group_label) ?? [];
    list.push(entry);
    groups.set(entry.group_label, list);
  }

  return (
    <div className="flex flex-col gap-8">
      <header>
        <p className="eyebrow flex items-center gap-2.5 text-brand">
          <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
          My integrations
        </p>
        <h1 className="display mt-4 text-3xl font-semibold">Connected applications</h1>
        <p className="mt-3 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Connect the tools you already use. JUTSU evaluates connected content against
          organisational privacy, relevance and access policies before anything becomes
          part of organisational memory — connecting an application does not make
          everything in it searchable.
        </p>
      </header>

      {catalogue.error ? (
        <FailureState
          failure={classifyApiError(catalogue.error)}
          onRetry={() => void catalogue.refetch()}
          deniedWhat="managing your integrations"
        />
      ) : catalogue.isPending ? (
        <LoadingRegion label="Loading your integrations.">
          <div className="grid gap-3 sm:grid-cols-2">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-40" />
            ))}
          </div>
        </LoadingRegion>
      ) : (
        [...groups.entries()].map(([label, entries]) => (
          <section key={label} aria-labelledby={`group-${label}`} className="flex flex-col gap-3">
            <h2
              id={`group-${label}`}
              className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground"
            >
              · {label}
            </h2>
            <ul className="grid gap-3 sm:grid-cols-2">
              {entries.map((entry) => (
                <ConnectorCard key={entry.id} entry={entry} />
              ))}
            </ul>
          </section>
        ))
      )}
    </div>
  );
}
