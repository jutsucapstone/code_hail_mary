"use client";

import { useCallback, useEffect, useState } from "react";
import { Building2, IdCard, ShieldCheck, UserRound } from "lucide-react";

import { useCapabilities } from "@/components/admin/admin-shell";
import { ErrorState, LoadingRegion, Skeleton } from "@/components/states";
import { ApiError, api } from "@/lib/api";
import type { components } from "@/lib/api-schema";

type Organisation = components["schemas"]["OrganisationProfile"];

/**
 * The organisation overview.
 *
 * Every figure here is counted in Postgres under the tenant scope, so it is this
 * organisation's rows and nobody else's — row-level security does the filtering rather
 * than a WHERE clause someone has to remember. Nothing is estimated, nothing is
 * illustrative, and nothing renders until the real numbers arrive (§4.11).
 *
 * "Administrators" is derived from the seeded permission matrix — anyone who may invite
 * or assign roles — rather than from a hardcoded list of role names, so adding a role
 * later cannot leave this count quietly wrong.
 */
export default function AdminOverviewPage() {
  const capabilities = useCapabilities();
  const [organisation, setOrganisation] = useState<Organisation | null>(null);
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(
    null,
  );

  // Every state update happens in a promise callback rather than in the effect body.
  // Clearing the error up front would be a synchronous setState inside the effect, which
  // React flags because it can cascade renders — so success clears it instead.
  const load = useCallback(() => {
    api
      .currentOrganisation()
      .then((profile) => {
        setOrganisation(profile);
        setFailure(null);
      })
      .catch((error: unknown) => {
        setFailure({
          message:
            error instanceof ApiError
              ? error.message
              : "We could not reach the service.",
          requestId: error instanceof ApiError ? error.requestId : undefined,
        });
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (failure) {
    return (
      <ErrorState
        message={failure.message}
        requestId={failure.requestId}
        onRetry={load}
      />
    );
  }

  if (!organisation) {
    return (
      <LoadingRegion label="Loading your organisation.">
        <div className="flex flex-col gap-6">
          <Skeleton className="h-10 w-72" />
          <div className="grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-3">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-28 bg-background" />
            ))}
          </div>
        </div>
      </LoadingRegion>
    );
  }

  const stats = [
    { id: "people", icon: UserRound, value: organisation.members.total, label: "People" },
    { id: "active", icon: ShieldCheck, value: organisation.members.active, label: "Active" },
    {
      id: "pending",
      icon: IdCard,
      value: organisation.members.invited,
      label: "Awaiting first sign-in",
    },
  ];

  return (
    <div className="flex flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <header>
        <p className="eyebrow flex items-center gap-2.5 text-brand">
          <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
          Overview
        </p>
        <h1 className="display mt-4 text-3xl font-semibold [@media(max-height:820px)]:mt-2 [@media(max-height:820px)]:text-2xl sm:text-4xl">
          {organisation.name}
        </h1>
        <p className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground">
          {organisation.domain ? <span>{organisation.domain}</span> : null}
          <span aria-hidden="true">·</span>
          <span>{organisation.status}</span>
          {organisation.size_band ? (
            <>
              <span aria-hidden="true">·</span>
              <span>{organisation.size_band} people</span>
            </>
          ) : null}
        </p>
      </header>

      <section aria-labelledby="people-heading">
        <h2 id="people-heading" className="sr-only">
          People
        </h2>
        <dl className="grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-3">
          {stats.map((stat) => (
            <div key={stat.id} className="flex flex-col gap-3 bg-background p-6 [@media(max-height:820px)]:gap-2 [@media(max-height:820px)]:p-4 lg:p-7">
              <span
                aria-hidden="true"
                className="flex size-9 items-center justify-center rounded-lg border border-hairline-strong bg-surface text-brand"
              >
                <stat.icon className="size-4" />
              </span>
              {/* dd before dt so the figure reads first visually; the pairing is still
                  correct for assistive technology, which follows the markup. */}
              <dd className="display text-3xl font-semibold tabular-nums">{stat.value}</dd>
              <dt className="text-sm text-muted-foreground">{stat.label}</dt>
            </div>
          ))}
        </dl>
      </section>

      <section aria-labelledby="identity-heading" className="flex flex-col gap-4">
        <h2 id="identity-heading" className="display text-xl font-semibold sm:text-2xl">
          Your access
        </h2>
        <dl className="grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-2">
          <div className="flex flex-col gap-1.5 bg-background p-6 [@media(max-height:820px)]:p-4">
            <dt className="text-sm text-muted-foreground">Your JUTSU ID</dt>
            <dd className="font-mono text-sm text-foreground">
              {capabilities.jutsu_id ?? "Not issued"}
            </dd>
          </div>
          <div className="flex flex-col gap-1.5 bg-background p-6 [@media(max-height:820px)]:p-4">
            <dt className="text-sm text-muted-foreground">Your role</dt>
            <dd className="text-sm text-foreground">{capabilities.role}</dd>
          </div>
          <div className="flex flex-col gap-1.5 bg-background p-6 [@media(max-height:820px)]:p-4">
            <dt className="text-sm text-muted-foreground">Organisation ID</dt>
            <dd className="break-all font-mono text-xs text-muted-foreground">
              {organisation.id}
            </dd>
          </div>
          <div className="flex flex-col gap-1.5 bg-background p-6 [@media(max-height:820px)]:p-4">
            <dt className="text-sm text-muted-foreground">Administrators</dt>
            <dd className="text-sm text-foreground tabular-nums">
              {organisation.members.admins}
            </dd>
          </div>
        </dl>
      </section>

      <section
        aria-labelledby="next-heading"
        className="flex items-start gap-4 rounded-2xl border border-hairline bg-surface/40 p-6 [@media(max-height:820px)]:p-4"
      >
        <span
          aria-hidden="true"
          className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-hairline-strong bg-surface text-brand"
        >
          <Building2 className="size-4" />
        </span>
        <div>
          <h2 id="next-heading" className="display text-lg font-semibold">
            What happens next
          </h2>
          <p className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
            Inviting people works now — send someone an invitation from Employees and
            their JUTSU ID is issued when they accept, not before. Connecting your tools
            is the next step and is not built yet; the sections in the sidebar that are
            still to come are listed with the slice that delivers them rather than
            showing figures that are not real.
          </p>
        </div>
      </section>
    </div>
  );
}
