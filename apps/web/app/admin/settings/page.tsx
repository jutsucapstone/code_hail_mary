"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { useCapabilities } from "@/components/admin/admin-shell";
import { PageHeader } from "@/components/admin/page-scaffold";
import { Field } from "@/components/pilot/field";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import {
  FailureState,
  LoadingRegion,
  PermissionDenied,
  Skeleton,
} from "@/components/states";
import { api } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import { queryKeys } from "@/lib/query";
import { can } from "@/lib/permissions";

/**
 * Organisation settings.
 *
 * One editable field, and that is honest rather than thin: the name is display data,
 * while the domain anchors the one-organisation-per-domain rule and the email
 * verification trust chain. Changing the domain is an identity operation with its own
 * ceremony, so the API refuses it and this page does not pretend otherwise.
 */
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

  if (!mayUpdate) {
    return <PermissionDenied what="permission to change organisation settings" />;
  }

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

      {organisation.error ? (
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
    </div>
  );
}
