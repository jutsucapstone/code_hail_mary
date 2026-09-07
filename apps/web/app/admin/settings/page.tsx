"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { useCapabilities } from "@/components/admin/admin-shell";
import { PageHeader, When } from "@/components/admin/page-scaffold";
import { Field } from "@/components/pilot/field";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import {
  FailureState,
  LoadingRegion,
  PermissionDenied,
  Skeleton,
} from "@/components/states";
import { api, type SyncSchedule } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import { queryKeys } from "@/lib/query";
import { can } from "@/lib/permissions";

/**
 * Organisation settings.
 *
 * Two things live here, and they are gated differently. The name is display data whose
 * only editor is an administrator — the domain anchors the one-organisation-per-domain
 * rule and the email verification trust chain, so the API refuses to change it and this
 * page does not pretend otherwise. The nightly sync schedule is readable by anyone who
 * may manage their own integrations and writable only with `sync:schedule_manage`, so a reader
 * without it still sees when the organisation's connected tools are read.
 */

/** `GET /v1/orgs/current/sync-schedule`. Kept here because two surfaces read it and only
 *  this one invalidates it; an invalidation that names a different key is a form that
 *  saves and a panel that never updates. */
const SYNC_SCHEDULE_KEY = ["org", "sync-schedule"] as const;

const HOURS = Array.from({ length: 24 }, (_, hour) => hour);

/**
 * Every IANA zone this browser's ICU knows.
 *
 * The control offers these rather than a text box so nobody can type a name the server
 * will refuse. `supportedValuesOf` is not universal; where it is missing the field falls
 * back to free text, which is worse but honest — a hand-written short list would quietly
 * exclude zones the organisation is entitled to choose.
 */
const TIME_ZONES: readonly string[] | null =
  typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : null;

/**
 * An hour of the day, 0–23, as a wall clock reads it.
 *
 * Formatted in UTC against a UTC instant: the number names an hour in the organisation's
 * own zone, so rendering it through the reader's zone would shift the very figure it is
 * naming.
 */
function hourLabel(hour: number): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "UTC",
  }).format(new Date(Date.UTC(2001, 0, 1, hour)));
}

/**
 * An instant on the ORGANISATION's clock, with the zone named beside it.
 *
 * The reader may be anywhere. "01:00" is an hour in the organisation's zone and nobody
 * else's, so showing it in the reader's own would be a confident wrong answer — and
 * showing it in the right one without saying which zone that is only moves the guess.
 */
function inOrgZone(iso: string, timeZone: string): string {
  try {
    const shown = new Intl.DateTimeFormat(undefined, {
      timeZone,
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(iso));
    return `${shown}, ${timeZone}`;
  } catch {
    // A zone this browser cannot resolve. Falling back to the reader's own clock would
    // be the one wrong answer available, so fall back to the raw instant instead.
    return `${iso} (${timeZone})`;
  }
}

/** One label/value row, matching the domain and status pair above it. */
function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5 bg-background p-5">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="text-sm text-foreground">{children}</dd>
    </div>
  );
}

function ScheduleFacts({
  schedule,
  mayReadHistory,
}: {
  schedule: SyncSchedule;
  mayReadHistory: boolean;
}) {
  return (
    <>
      <dl className="mt-5 grid gap-px overflow-clip rounded-xl border border-hairline bg-hairline sm:grid-cols-3">
        <Fact label="Automatic sync">{schedule.enabled ? "On" : "Off"}</Fact>
        <Fact label="Runs at">{`${hourLabel(schedule.hour_local)}, ${schedule.timezone}`}</Fact>
        <Fact label="Next run">
          {schedule.enabled && schedule.next_sync_at
            ? inOrgZone(schedule.next_sync_at, schedule.timezone)
            : "Not scheduled — automatic sync is off."}
        </Fact>
      </dl>

      {schedule.last_started_at ? (
        <dl className="mt-4 grid gap-px overflow-clip rounded-xl border border-hairline bg-hairline sm:grid-cols-3">
          <Fact label="Last run started">
            <When iso={schedule.last_started_at} />
          </Fact>
          <Fact label="Finished">
            <When iso={schedule.last_finished_at} />
          </Fact>
          <Fact label="Outcome">{schedule.last_outcome ?? "—"}</Fact>
          <Fact label="Connections read">
            <span className="tabular-nums">{schedule.last_connections ?? "—"}</span>
          </Fact>
          <Fact label="Jobs queued">
            <span className="tabular-nums">{schedule.last_enqueued ?? "—"}</span>
          </Fact>
        </dl>
      ) : mayReadHistory ? (
        <p className="mt-4 text-sm text-muted-foreground">
          No automatic run has been recorded yet.
        </p>
      ) : null}
      {/* Nothing at all for a reader without `org:read`: the API redacts the run history
          to nulls for them, so "no run recorded" would be this page inventing a fact out
          of a redaction. */}
    </>
  );
}

function ScheduleForm({ schedule }: { schedule: SyncSchedule }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: api.updateSyncSchedule,
    onSuccess: (updated) => {
      setError(null);
      toast.success(
        updated.enabled
          ? `Nightly sync set to ${hourLabel(updated.hour_local)}, ${updated.timezone}.`
          : "Automatic sync is now off.",
      );
      void queryClient.invalidateQueries({ queryKey: SYNC_SCHEDULE_KEY });
    },
    // The server owns validation — an unknown timezone comes back as a 422 naming the
    // zone, and the sentence it wrote is the one that tells the reader what to change.
    onError: (mutationError: unknown) => setError(classifyApiError(mutationError).message),
  });

  // A zone the browser does not list would otherwise be silently replaced by whichever
  // option happens to sit first, turning "save the hour" into "move the schedule".
  const zones =
    TIME_ZONES === null
      ? null
      : TIME_ZONES.includes(schedule.timezone)
        ? TIME_ZONES
        : [schedule.timezone, ...TIME_ZONES];

  function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    save.mutate({
      timezone: String(form.get("timezone") ?? ""),
      hour_local: Number(form.get("hour_local")),
      // An unticked box sends no field at all, which is exactly `enabled: false`.
      enabled: form.get("enabled") === "on",
    });
  }

  return (
    <form onSubmit={onSubmit} className="mt-6 flex flex-col gap-4">
      <label className="flex items-center gap-2.5 text-sm text-foreground">
        <input
          type="checkbox"
          name="enabled"
          defaultChecked={schedule.enabled}
          className="size-4 rounded border-hairline-strong accent-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        />
        Sync connected tools automatically every night
      </label>

      <div className="flex flex-col gap-4 sm:flex-row sm:items-end">
        <div className="flex flex-col gap-2 sm:w-40">
          <label htmlFor="sync-hour" className="text-sm font-medium text-foreground">
            Hour
          </label>
          <select
            id="sync-hour"
            name="hour_local"
            defaultValue={schedule.hour_local}
            className="h-11 rounded-xl border border-hairline-strong bg-surface/40 px-3.5 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            {HOURS.map((hour) => (
              <option key={hour} value={hour}>
                {hourLabel(hour)}
              </option>
            ))}
          </select>
        </div>

        {zones ? (
          <div className="flex flex-1 flex-col gap-2">
            <label htmlFor="sync-timezone" className="text-sm font-medium text-foreground">
              Timezone
            </label>
            <select
              id="sync-timezone"
              name="timezone"
              defaultValue={schedule.timezone}
              className="h-11 rounded-xl border border-hairline-strong bg-surface/40 px-3.5 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
            >
              {zones.map((zone) => (
                <option key={zone} value={zone}>
                  {zone}
                </option>
              ))}
            </select>
          </div>
        ) : (
          <Field
            id="sync-timezone"
            name="timezone"
            label="Timezone"
            required
            maxLength={64}
            defaultValue={schedule.timezone}
            hint="An IANA zone name, such as Asia/Kolkata. The server refuses any name it does not recognise."
            className="flex-1"
          />
        )}

        <SubmitButton
          pending={save.isPending}
          pendingLabel="Updating…"
          className="sm:w-44"
        >
          Update schedule
        </SubmitButton>
      </div>

      {error ? <FormError message={error} /> : null}
    </form>
  );
}

function NightlySyncSection({
  maySetSchedule,
  mayReadHistory,
}: {
  /**
   * `sync:schedule_manage`, not `org:update`.
   *
   * The clock has four owners — Owner, Super Admin, IT Admin and HR Admin — and
   * `org:update` reaches only the first three. Gating this form on that permission left
   * an HR Admin looking at a schedule they are entitled to set and no way to set it,
   * while the alternative (granting HR `org:update`) would have handed them the
   * organisation's name and its connection policies too.
   */
  maySetSchedule: boolean;
  mayReadHistory: boolean;
}) {
  const schedule = useQuery({
    queryKey: SYNC_SCHEDULE_KEY,
    queryFn: api.syncSchedule,
  });

  return (
    <section
      aria-labelledby="sync-schedule-heading"
      className="rounded-2xl border border-hairline bg-surface/40 p-6 sm:p-7"
    >
      <h2 id="sync-schedule-heading" className="display text-lg font-semibold">
        Nightly sync
      </h2>
      <p className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
        Connected applications are re-read once a day, at the hour you choose, in the
        organisation&apos;s own timezone. Every connector is read-only; nothing is ever
        written back.
      </p>

      {/* There is no empty state here on purpose: every organisation has a schedule row
          from the moment it exists (ADR 0018 §5), so an absent one is a failure. */}
      {schedule.error ? (
        <div className="mt-5">
          <FailureState
            failure={classifyApiError(schedule.error)}
            onRetry={() => void schedule.refetch()}
            deniedWhat="reading the synchronisation schedule"
          />
        </div>
      ) : schedule.isPending ? (
        <LoadingRegion label="Loading the synchronisation schedule.">
          <Skeleton className="mt-5 h-32" />
        </LoadingRegion>
      ) : (
        <>
          <ScheduleFacts schedule={schedule.data} mayReadHistory={mayReadHistory} />
          {maySetSchedule ? <ScheduleForm schedule={schedule.data} /> : null}
        </>
      )}
    </section>
  );
}

export default function SettingsPage() {
  const capabilities = useCapabilities();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const mayUpdate = can(capabilities, "org:update");

  const organisation = useQuery({
    queryKey: queryKeys.organisation,
    queryFn: api.currentOrganisation,
    enabled: mayUpdate,
  });

  const rename = useMutation({
    mutationFn: (name: string) => api.renameOrganisation({ name }),
    onSuccess: (result) => {
      setError(null);
      toast.success(`Renamed to ${result.name}.`);
      // The profile card, the overview heading and this form all show the name; refetch
      // rather than patching three caches by hand.
      void queryClient.invalidateQueries({ queryKey: queryKeys.organisation });
    },
    onError: (mutationError: unknown) => {
      setError(classifyApiError(mutationError).message);
    },
  });

  function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = String(new FormData(event.currentTarget).get("name") ?? "").trim();
    if (name) rename.mutate(name);
  }

  return (
    <div className="flex flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="Access" title="Organisation">
        The name is how your organisation appears across the console and in email. The
        domain is fixed: it anchors who may register and how addresses are verified.
      </PageHeader>

      {/* The denial is now the name card's rather than the page's: the sync schedule
          below it is readable by every role, and returning early would have hidden from
          an employee the one fact on this page that is about their own data. */}
      {!mayUpdate ? (
        <PermissionDenied what="permission to change organisation settings" />
      ) : organisation.error ? (
        <FailureState
          failure={classifyApiError(organisation.error)}
          onRetry={() => void organisation.refetch()}
          deniedWhat="reading organisation settings"
        />
      ) : organisation.isPending ? (
        <LoadingRegion label="Loading organisation settings.">
          <Skeleton className="h-40" />
        </LoadingRegion>
      ) : (
        <section
          aria-labelledby="org-name-heading"
          className="rounded-2xl border border-hairline bg-surface/40 p-6 sm:p-7"
        >
          <h2 id="org-name-heading" className="display text-lg font-semibold">
            Name
          </h2>
          <form onSubmit={onSubmit} className="mt-5 flex flex-col gap-4 sm:flex-row sm:items-end">
            <Field
              id="org-name"
              name="name"
              label="Organisation name"
              required
              maxLength={255}
              defaultValue={organisation.data.name}
              className="flex-1"
            />
            <SubmitButton pending={rename.isPending} pendingLabel="Saving…" className="sm:w-32">
              Save
            </SubmitButton>
          </form>
          {error ? (
            <div className="mt-4">
              <FormError message={error} />
            </div>
          ) : null}

          <dl className="mt-8 grid gap-px overflow-clip rounded-xl border border-hairline bg-hairline sm:grid-cols-2">
            <div className="flex flex-col gap-1.5 bg-background p-5">
              <dt className="text-sm text-muted-foreground">Domain</dt>
              <dd className="font-mono text-sm text-foreground">
                {organisation.data.domain ?? "—"}
              </dd>
            </div>
            <div className="flex flex-col gap-1.5 bg-background p-5">
              <dt className="text-sm text-muted-foreground">Status</dt>
              <dd className="text-sm text-foreground">{organisation.data.status}</dd>
            </div>
          </dl>
        </section>
      )}

      <NightlySyncSection
        maySetSchedule={can(capabilities, "sync:schedule_manage")}
        mayReadHistory={can(capabilities, "org:read")}
      />
    </div>
  );
}
