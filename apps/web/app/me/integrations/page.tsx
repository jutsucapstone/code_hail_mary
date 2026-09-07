"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Pill, When } from "@/components/admin/page-scaffold";
import { EmptyState, FailureState, LoadingRegion, Skeleton } from "@/components/states";
import { Field } from "@/components/pilot/field";
import { api, type IntegrationEntry, type SyncSchedule } from "@/lib/api";
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
 *
 * Both ends of that round trip come back here, and both are announced. The callback
 * redirects to `?connected=<provider>` when the exchange succeeded and
 * `?connect_error=<reason>` when it did not, and a page that renders nothing for the
 * second one leaves somebody who pressed Deny staring at an unchanged screen wondering
 * what they broke.
 */

/** `GET /v1/orgs/current/sync-schedule`, keyed as the settings page keys it. */
const SYNC_SCHEDULE_KEY = ["org", "sync-schedule"] as const;

/**
 * When these tools are read again, on the ORGANISATION's clock.
 *
 * The zone is named beside the time because the reader may be in another one, and "1:00
 * AM" without it is a promise about an hour that never arrives for them. Only the two
 * fields an employee is actually given: the run history is redacted to nulls for anyone
 * without `org:read`, so there is nothing here to render from it.
 */
function syncSentence(schedule: SyncSchedule): string {
  if (!schedule.enabled || !schedule.next_sync_at) {
    return "Automatic syncing is switched off for your organisation. You can still sync any connected tool yourself.";
  }
  let when: string;
  try {
    when = new Intl.DateTimeFormat(undefined, {
      timeZone: schedule.timezone,
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(schedule.next_sync_at));
  } catch {
    // A zone this browser cannot resolve. The reader's own clock would be the one
    // definitely wrong answer, so show the instant as it arrived instead.
    when = schedule.next_sync_at;
  }
  return `Your connected tools are read again automatically at ${when}, ${schedule.timezone}.`;
}

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

/** What a stored `last_error_kind` means to the person who owns the connection. */
const ERROR_SENTENCES: Record<string, string> = {
  reauth_required:
    "The provider no longer honours this authorisation. Reconnect to continue syncing.",
  sync_unavailable: "Syncing is not available for this provider on this deployment yet.",
  // Written by the callback when the browser came back without a code, so the attempt
  // never got as far as a sync. Without its own sentence it falls to the one below and
  // blames a run that never happened.
  authorization_denied:
    "The authorisation was not completed, so nothing was connected. Reconnect to try again.",
};

function errorSentence(kind: string): string {
  return ERROR_SENTENCES[kind] ?? `Last sync problem: ${kind.replaceAll("_", " ")}.`;
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
          <dt className="text-muted-foreground">Documents indexed</dt>
          <dd className="tabular-nums text-foreground">{connection.document_count}</dd>
        </dl>
      ) : null}

      {connection?.last_error_kind ? (
        <p className="text-xs text-graph">{errorSentence(connection.last_error_kind)}</p>
      ) : null}

      {connection ? (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand">
            Manage
          </summary>
          {/* What the grant actually says — the scopes the provider showed at consent,
              verbatim. Read-only by registry construction; showing them is how an owner
              audits their own connection (§2). */}
          <dl className="mt-2 flex flex-col gap-1.5 border-l border-hairline pl-3">
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">Connected</dt>
              <dd className="text-foreground">
                {connection.connected_at ? <When iso={connection.connected_at} /> : "—"}
              </dd>
            </div>
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">Authorised scopes (read-only)</dt>
              <dd className="font-mono text-[0.625rem] leading-relaxed text-muted-foreground">
                {connection.scopes.length > 0 ? connection.scopes.join(" · ") : "—"}
              </dd>
            </div>
          </dl>
        </details>
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
            {/*
              Every status that is not `connected` gets the primary action, not just the
              two failure ones. A connection sits at `connecting` from the moment the
              authorize URL is minted until the callback lands, so anyone who closed the
              provider's consent screen, or whose callback failed, was left with a
              "connecting" pill and a single Disconnect button — a dead end reachable by
              doing nothing wrong, on the screen whose whole purpose is connecting.
              Starting again is safe: the API mints a fresh state and PKCE pair, and the
              stale attempt expires on its own.
            */}
            {connection.status !== "connected" && entry.configured && entry.allowed ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => connect.mutate()}
                className="rounded-lg bg-brand px-3.5 py-2 text-sm font-medium text-brand-foreground transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
              >
                {connect.isPending
                  ? "Starting…"
                  : connection.status === "connecting"
                    ? "Continue connecting"
                    : "Reconnect"}
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
  const connectErrorParam = searchParams.get("connect_error");
  const [query, setQuery] = useState("");

  const catalogue = useQuery({
    queryKey: ["integrations"],
    queryFn: api.integrations,
  });

  // A supporting line, not the page's subject. It has no loading state and no failure
  // state on purpose: a schedule that will not load must not put an error banner over
  // the connections, which are what someone came here for and which load independently.
  const schedule = useQuery({
    queryKey: SYNC_SCHEDULE_KEY,
    queryFn: api.syncSchedule,
  });

  useEffect(() => {
    // The OAuth callback redirects back here with ?connected=<provider>. Announce it
    // once; the catalogue itself already shows the new state.
    if (connectedParam) {
      toast.success(`Connected. JUTSU can now see what ${connectedParam} lets your account see.`);
      window.history.replaceState(null, "", "/me/integrations");
    }
  }, [connectedParam]);

  // `connect_error` carries the provider id when the abandoned attempt matched a row of
  // the caller's own, and a sanitised provider code (`access_denied`, `denied`) when it
  // matched nothing. Only the catalogue can tell those two apart, so the name is looked
  // up rather than printed — an unmatched value is somebody else's error code, not a
  // tool anybody here would recognise.
  const refusedName = connectErrorParam
    ? (catalogue.data?.items.find((item) => item.id === connectErrorParam)?.name ?? null)
    : null;

  useEffect(() => {
    // Waits for the catalogue so the sentence can name the tool. The reason is NOT
    // named: the callback takes this branch both when the person pressed Deny and when
    // the provider refused for its own reasons, and it records one `last_error_kind`
    // for both — so "you cancelled" would be a guess about which of the two happened.
    // `isSuccess`, not `!isPending`: a catalogue that FAILED is also not pending,
    // and firing here would stack an unrelated toast on top of the page's own
    // failure state. The row's sentence still carries the refusal either way.
    if (!connectErrorParam || !catalogue.isSuccess) return;
    toast.error(
      refusedName
        ? `${refusedName} was not connected. The authorisation did not complete, and nothing was read from it.`
        : "That connection was not completed. Nothing was connected and nothing was read.",
    );
    window.history.replaceState(null, "", "/me/integrations");
  }, [connectErrorParam, refusedName, catalogue.isSuccess]);

  // The search filters the real catalogue; it never invents an entry. A term that
  // matches nothing gets an honest "not supported yet" state instead of a blank grid,
  // because the person typing "Notion" deserves an answer, not an absence (§2).
  const needle = query.trim().toLowerCase();
  const groups = new Map<string, IntegrationEntry[]>();
  let shown = 0;
  for (const entry of catalogue.data?.items ?? []) {
    if (
      needle &&
      !entry.name.toLowerCase().includes(needle) &&
      !entry.description.toLowerCase().includes(needle) &&
      !entry.group_label.toLowerCase().includes(needle)
    ) {
      continue;
    }
    shown += 1;
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
        {schedule.data ? (
          <p className="mt-3 max-w-prose text-pretty text-sm leading-relaxed text-foreground">
            {syncSentence(schedule.data)}
          </p>
        ) : null}
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
        <>
          <div className="max-w-sm">
            <Field
              id="integration-search"
              name="integration-search"
              type="search"
              label="Search platforms"
              placeholder="Search any platform or tool…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>

          {shown === 0 ? (
            <EmptyState title={`"${query.trim()}" is not supported yet`}>
              <p>
                It is not in the connector catalogue this deployment can serve. New
                connectors are added platform-side so every one stays read-only and
                policy-governed — ask your administrator to request it, and it will
                appear here the release it lands.
              </p>
            </EmptyState>
          ) : null}

          {[...groups.entries()].map(([label, entries]) => {
            // `aria-labelledby` holds a space-separated id list, so a label with
            // spaces would reference several ids that exist nowhere.
            const headingId = `group-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
            return (
              <section key={label} aria-labelledby={headingId} className="flex flex-col gap-3">
                <h2
                  id={headingId}
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
            );
          })}
        </>
      )}
    </div>
  );
}
