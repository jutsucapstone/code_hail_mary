"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { useCapabilities } from "@/components/admin/admin-shell";
import { KtAttachments } from "@/components/admin/kt-attachments";
import { CopyButton } from "@/components/copy-button";
import {
  LoadMore,
  PageHeader,
  Pill,
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
import { Field } from "@/components/pilot/field";
import { FormError } from "@/components/pilot/submit-button";
import { api, type AuditEntry, type KtAdmin, type KtAdminPage } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import type { components } from "@/lib/api-schema";
import { can } from "@/lib/permissions";

type Employee = components["schemas"]["Employee"];

/**
 * Knowledge Transfer — the admin lifecycle.
 *
 * Creating a package creates no access: what its recipient reads inside the workspace
 * is bounded by their own grants, per query. The wizard's scope step offers only what
 * `GET /v1/kt/scopes` says the backend can serve (§13) — categories arrive as the
 * platform grows them, and this page never invents one.
 *
 * Statuses are derived server-side in one place, so this list, the detail and the
 * recipient's open path cannot disagree about what a package currently is.
 *
 * The detail panel also carries the two changes an administrator may make to a live
 * package — extend its expiry, or re-address one nobody has opened — through the one
 * `PATCH` the server exposes; what it offers follows the server's own rules.
 *
 * The detail panel's activity list is the audit trail filtered to one package: opens,
 * claims, refused attempts and lifecycle changes, each as the trail records it. Actors
 * appear as JUTSU IDs or an actor type — the trail carries no email address (§4.9), and
 * this page never looks one up to "enrich" a row.
 */

const STATUS_TONE: Record<string, "good" | "attention" | "bad" | "neutral"> = {
  active: "good",
  claimed: "attention",
  expired: "neutral",
  revoked: "bad",
  completed: "neutral",
};

const VALIDITY_CHOICES = [7, 30, 60, 90] as const;
const PERIOD_CHOICES = [
  { label: "Last 3 months", days: 92 },
  { label: "Last 6 months", days: 183 },
  { label: "Last 12 months", days: 366 },
  { label: "Full history", days: null },
] as const;

const SCOPE_LABELS: Record<string, string> = {
  documents: "Documents",
  profile: "Role & profile",
  decisions: "Decisions",
  people: "Key contacts",
  projects: "Projects",
  meetings: "Meetings",
  responsibilities: "Responsibilities",
};

/** How a package names its recipient before and after the first open binds it. */
function recipientLabel(pkg: KtAdmin): string {
  return pkg.recipient_email ?? (pkg.claimed_at ? "Claimed" : "Bound to first opener");
}

function OutcomePill({ outcome }: { outcome: string }) {
  const tone = outcome === "success" ? "good" : outcome === "denied" ? "attention" : "bad";
  return <Pill tone={tone}>{outcome}</Pill>;
}

function DetailField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5 bg-background p-4">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm text-foreground">{children}</dd>
    </div>
  );
}

const EXTENSION_CHOICES = [7, 30, 60, 90] as const;

/**
 * Extend or re-address, through `PATCH /v1/kt/{id}`.
 *
 * What is offered follows the server's rules rather than guessing at them: a revoked or
 * completed package is terminal and gets neither control; a package that has bound its
 * recipient cannot be re-addressed. The server refuses both anyway (409) — the panel just
 * never offers a button whose only possible outcome is that refusal.
 */
function PackageControls({ pkg, onChanged }: { pkg: KtAdmin; onChanged: () => void }) {
  const queryClient = useQueryClient();
  const [extendDays, setExtendDays] = useState<number>(30);
  const [recipient, setRecipient] = useState("");
  const [error, setError] = useState<string | null>(null);

  const update = useMutation({
    mutationFn: (body: Parameters<typeof api.ktUpdate>[1]) => api.ktUpdate(pkg.id, body),
    onSuccess: (_updated, body) => {
      setError(null);
      if (body.recipient_email) setRecipient("");
      toast.success(body.extend_days ? "Expiry extended." : "Package re-addressed.");
      // The record and its trail re-read; the list is the caller's to refresh.
      void queryClient.invalidateQueries({ queryKey: ["kt", pkg.id] });
      onChanged();
    },
    onError: (mutationError: unknown) => setError(classifyApiError(mutationError).message),
  });

  if (pkg.status === "revoked" || pkg.status === "completed") return null;
  const readdressable = pkg.claimed_at === null;

  const buttonClass =
    "self-start rounded-lg border border-hairline-strong px-3.5 py-2 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60";

  return (
    <section aria-labelledby="kt-manage-heading" className="flex flex-col gap-3">
      <h3 id="kt-manage-heading" className="text-sm font-medium text-foreground">
        Manage
      </h3>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-3 rounded-xl border border-hairline bg-background p-4">
          <label htmlFor="kt-extend-days" className="text-xs text-muted-foreground">
            Extend expiry by
          </label>
          <div className="flex flex-wrap gap-2">
            <select
              id="kt-extend-days"
              value={extendDays}
              onChange={(event) => setExtendDays(Number(event.target.value))}
              className="h-10 rounded-lg border border-hairline-strong bg-surface/40 px-3 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
            >
              {EXTENSION_CHOICES.map((days) => (
                <option key={days} value={days}>
                  {days} days
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={update.isPending}
              aria-busy={update.isPending}
              onClick={() => {
                setError(null);
                update.mutate({ extend_days: extendDays });
              }}
              className={buttonClass}
            >
              Extend expiry
            </button>
          </div>
          <p className="text-xs text-muted-foreground">
            From the later of now and the current expiry, never past a year from today. A
            lapsed package reopens for its recipient.
          </p>
        </div>

        {readdressable ? (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              const value = recipient.trim();
              if (!value || update.isPending) return;
              setError(null);
              update.mutate({ recipient_email: value });
            }}
            className="flex flex-col gap-3 rounded-xl border border-hairline bg-background p-4"
          >
            <Field
              id="kt-readdress"
              name="readdress"
              type="email"
              label="Re-address to"
              placeholder="The person who should open it"
              value={recipient}
              onChange={(event) => setRecipient(event.target.value)}
            />
            <button
              type="submit"
              disabled={update.isPending || recipient.trim().length === 0}
              aria-busy={update.isPending}
              className={buttonClass}
            >
              Re-address
            </button>
            <p className="text-xs text-muted-foreground">
              Only until somebody opens it: the first open binds the package to its
              recipient, and the address then cannot change.
            </p>
          </form>
        ) : (
          <div className="flex flex-col gap-1.5 rounded-xl border border-hairline bg-background p-4">
            <p className="text-xs text-muted-foreground">Re-address</p>
            <p className="text-sm text-muted-foreground">
              Bound to its recipient at first open, so it can no longer be re-addressed.
              Revoke it and create a new package instead.
            </p>
          </div>
        )}
      </div>
      {error ? <FormError message={error} /> : null}
    </section>
  );
}

/**
 * One package's record and its slice of the audit trail.
 *
 * `canReadAudit` is decided by the caller from capabilities: without `audit:read` the
 * activity request would only come back 403, so the panel says so in one sentence
 * instead of rendering a denial notice for a request it never needed to make.
 */
function PackageDetails({
  id,
  canReadAudit,
  onClose,
  onChanged,
}: {
  id: string;
  canReadAudit: boolean;
  onClose: () => void;
  /** After an extension or a re-address: the list's expiry and recipient columns changed. */
  onChanged: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const detail = useQuery({ queryKey: ["kt", id, "detail"], queryFn: () => api.ktGet(id) });
  const activity = useQuery({
    queryKey: ["kt", id, "activity"],
    queryFn: () => api.audit({ resource_type: "kt_package", resource_id: id, limit: 20 }),
    enabled: canReadAudit,
  });

  // The panel opens beneath a table the reader was just working in; moving focus to
  // its heading is what tells a keyboard or screen-reader user that anything happened.
  useEffect(() => {
    headingRef.current?.focus();
  }, [id]);

  return (
    <section
      id="kt-details"
      aria-labelledby="kt-details-heading"
      className="flex flex-col gap-5 rounded-2xl border border-hairline bg-surface/40 p-6 sm:p-7"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h2
          id="kt-details-heading"
          ref={headingRef}
          tabIndex={-1}
          className="display text-lg font-semibold focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand"
        >
          Package details
        </h2>
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg border border-hairline-strong px-3 py-1.5 text-sm transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        >
          Close details
        </button>
      </div>

      {detail.error ? (
        <FailureState
          failure={classifyApiError(detail.error)}
          onRetry={() => void detail.refetch()}
          deniedWhat="reading this package"
        />
      ) : detail.isPending ? (
        <LoadingRegion label="Loading the package.">
          <div className="grid gap-2 sm:grid-cols-2">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-14" />
            ))}
          </div>
        </LoadingRegion>
      ) : (
        <dl className="grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-2 lg:grid-cols-3">
          <DetailField label="Status">
            <Pill tone={STATUS_TONE[detail.data.status] ?? "neutral"}>{detail.data.status}</Pill>
          </DetailField>
          <DetailField label="KT ID">
            <span className="font-mono text-[0.6875rem] uppercase tracking-[0.14em]">
              {detail.data.kt_code}
            </span>
          </DetailField>
          <DetailField label="Employee">
            {detail.data.subject_name ?? detail.data.subject_email}
          </DetailField>
          <DetailField label="Recipient">{recipientLabel(detail.data)}</DetailField>
          <DetailField label="Knowledge scope">
            <span className="flex flex-wrap gap-1.5">
              {detail.data.scope.map((category) => (
                <Pill key={category} tone="neutral">
                  {SCOPE_LABELS[category] ?? category}
                </Pill>
              ))}
            </span>
          </DetailField>
          <DetailField label="Knowledge period">
            {detail.data.period_start ? (
              <>
                <When iso={detail.data.period_start} /> — <When iso={detail.data.period_end} />
              </>
            ) : (
              "Full history"
            )}
          </DetailField>
          <DetailField label="Created">
            <When iso={detail.data.created_at} />
          </DetailField>
          <DetailField label="Expires">
            <When iso={detail.data.expires_at} />
          </DetailField>
          <DetailField label="Claimed at">
            <When iso={detail.data.claimed_at} />
          </DetailField>
          <DetailField label="Last activity">
            <When iso={detail.data.last_activity_at} />
          </DetailField>
        </dl>
      )}

      {detail.data ? <PackageControls pkg={detail.data} onChanged={onChanged} /> : null}

      {/* Which of the subject's own uploads travel with this package (ADR 0021).
          Placed under the lifecycle controls because it IS a lifecycle question: the
          grant these create lives and dies with the package above them. */}
      {detail.data ? (
        <KtAttachments
          packageId={id}
          closed={detail.data.status === "revoked" || detail.data.status === "completed"}
        />
      ) : null}

      <section aria-labelledby="kt-activity-heading" className="flex flex-col gap-3">
        <h3 id="kt-activity-heading" className="text-sm font-medium text-foreground">
          Activity
        </h3>
        <p className="max-w-prose text-xs text-muted-foreground">
          The audit trail for this package: opens, claims, refused attempts and lifecycle
          changes, newest first, up to the 20 most recent. In the trail, actors appear as
          JUTSU IDs, never as email addresses.
        </p>
        {!canReadAudit ? (
          <p className="text-sm text-muted-foreground">Your role cannot read the audit trail.</p>
        ) : activity.error ? (
          <FailureState
            failure={classifyApiError(activity.error)}
            onRetry={() => void activity.refetch()}
            deniedWhat="reading the audit trail"
          />
        ) : activity.isPending ? (
          <LoadingRegion label="Loading this package's activity.">
            <div className="flex flex-col gap-2">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-10" />
              ))}
            </div>
          </LoadingRegion>
        ) : activity.data.items.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No activity recorded for this package yet.
          </p>
        ) : (
          <ul className="flex flex-col divide-y divide-hairline rounded-xl border border-hairline">
            {activity.data.items.map((entry: AuditEntry) => (
              <li
                key={entry.id}
                className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-4 py-2.5 text-xs"
              >
                <span className="font-mono text-foreground">{entry.action}</span>
                <OutcomePill outcome={entry.outcome} />
                <span className="text-muted-foreground">
                  <When iso={entry.ts} />
                </span>
                <span className="font-mono text-muted-foreground">
                  {entry.actor_jutsu_id ?? entry.actor_type}
                </span>
              </li>
            ))}
          </ul>
        )}
        <Link
          href="/admin/audit"
          className="self-start rounded text-sm text-brand underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        >
          Open the full audit trail
        </Link>
      </section>
    </section>
  );
}

function CreateWizard({ onCreated }: { onCreated: (pkg: KtAdmin) => void }) {
  const [subjectQuery, setSubjectQuery] = useState("");
  // The whole person, not just an id: narrowing the search can filter the chosen
  // subject out of the current result page, and the review sentence must keep naming
  // them rather than reverting to "choose an employee".
  const [subject, setSubject] = useState<Employee | null>(null);
  const [scope, setScope] = useState<string[]>(["documents", "profile"]);
  const [periodDays, setPeriodDays] = useState<number | null>(null);
  const [validityDays, setValidityDays] = useState<number>(30);
  const [recipient, setRecipient] = useState("");
  const [error, setError] = useState<string | null>(null);

  const scopes = useQuery({ queryKey: ["kt", "scopes"], queryFn: api.ktScopes });
  const employees = useQuery({
    queryKey: ["employees", { q: subjectQuery || null, cursor: null }],
    queryFn: () => api.employees({ q: subjectQuery || null }),
  });

  const create = useMutation({
    mutationFn: () =>
      api.ktCreate({
        subject_user_id: subject!.id,
        scope,
        validity_days: validityDays,
        period_days: periodDays,
        recipient_email: recipient.trim() || null,
      }),
    onSuccess: (pkg) => onCreated(pkg),
    onError: (mutationError: unknown) => setError(classifyApiError(mutationError).message),
  });

  function toggleScope(category: string) {
    setScope((current) =>
      current.includes(category)
        ? current.filter((c) => c !== category)
        : [...current, category],
    );
  }

  return (
    <section
      aria-labelledby="kt-create-heading"
      className="flex flex-col gap-6 rounded-2xl border border-hairline bg-surface/40 p-6 sm:p-7"
    >
      <h2 id="kt-create-heading" className="display text-lg font-semibold">
        Create a knowledge-transfer package
      </h2>

      {/* Step 1 — the employee whose context is being packaged. */}
      <fieldset className="flex flex-col gap-3">
        <legend className="text-sm font-medium text-foreground">1 · Employee</legend>
        <Field
          id="kt-subject-search"
          name="subject"
          label="Search people"
          placeholder="Name or email"
          value={subjectQuery}
          onChange={(event) => setSubjectQuery(event.target.value)}
          className="sm:w-80"
        />
        {employees.error ? (
          <FailureState
            failure={classifyApiError(employees.error)}
            onRetry={() => void employees.refetch()}
            deniedWhat="searching the people in this organisation"
          />
        ) : employees.data ? (
          <ul className="flex max-h-44 flex-col gap-1 overflow-y-auto" aria-label="Employees">
            {employees.data.items.map((person) => (
              <li key={person.id}>
                <button
                  type="button"
                  aria-pressed={subject?.id === person.id}
                  onClick={() => setSubject(person)}
                  className={`w-full rounded-lg border px-3 py-2 text-left text-sm transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand ${
                    subject?.id === person.id
                      ? "border-brand/40 bg-brand/8 text-foreground"
                      : "border-hairline text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {person.display_name ?? person.email}
                  <span className="ml-2 text-xs text-muted-foreground">{person.email}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </fieldset>

      {/* Step 2 — scope, from the backend's own list, nothing invented. */}
      <fieldset className="flex flex-col gap-3">
        <legend className="text-sm font-medium text-foreground">2 · Knowledge scope</legend>
        {scopes.error ? (
          <FailureState
            failure={classifyApiError(scopes.error)}
            onRetry={() => void scopes.refetch()}
            deniedWhat="reading the supported knowledge scopes"
          />
        ) : null}
        <div className="flex flex-wrap gap-2">
          {(scopes.data?.supported ?? []).map((category) => (
            <label
              key={category}
              className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
                scope.includes(category)
                  ? "border-brand/40 bg-brand/8 text-foreground"
                  : "border-hairline-strong text-muted-foreground"
              }`}
            >
              <input
                type="checkbox"
                checked={scope.includes(category)}
                onChange={() => toggleScope(category)}
                className="accent-[var(--brand)]"
              />
              {SCOPE_LABELS[category] ?? category}
            </label>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          Every category is served from real data: documents under the recipient&apos;s
          own access, and the rest from evidence-anchored knowledge extraction.
        </p>
      </fieldset>

      {/* Steps 3 & 4 — period and validity. */}
      <div className="grid gap-6 sm:grid-cols-2">
        <fieldset className="flex flex-col gap-2">
          <legend className="text-sm font-medium text-foreground">3 · Time period</legend>
          <select
            aria-label="Knowledge period"
            value={periodDays === null ? "all" : String(periodDays)}
            onChange={(event) =>
              setPeriodDays(event.target.value === "all" ? null : Number(event.target.value))
            }
            className="h-11 rounded-xl border border-hairline-strong bg-surface/40 px-3.5 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            {PERIOD_CHOICES.map((choice) => (
              <option key={choice.label} value={choice.days === null ? "all" : choice.days}>
                {choice.label}
              </option>
            ))}
          </select>
        </fieldset>
        <fieldset className="flex flex-col gap-2">
          <legend className="text-sm font-medium text-foreground">4 · Package validity</legend>
          <select
            aria-label="Validity"
            value={validityDays}
            onChange={(event) => setValidityDays(Number(event.target.value))}
            className="h-11 rounded-xl border border-hairline-strong bg-surface/40 px-3.5 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            {VALIDITY_CHOICES.map((days) => (
              <option key={days} value={days}>
                {days} days
              </option>
            ))}
          </select>
        </fieldset>
      </div>

      <Field
        id="kt-recipient"
        name="recipient"
        type="email"
        label="Recipient email (optional)"
        placeholder="Bind the package to one person up front"
        value={recipient}
        onChange={(event) => setRecipient(event.target.value)}
        className="sm:w-96"
      />

      {/* Step 5 — review, in one sentence, then create. */}
      <div className="flex flex-col gap-3 rounded-xl border border-hairline bg-background p-4">
        <p className="text-sm text-muted-foreground">
          {subject
            ? `Package ${subject.display_name ?? subject.email}'s ${scope
                .map((c) => (SCOPE_LABELS[c] ?? c).toLowerCase())
                .join(" and ")} from ${
                periodDays === null ? "their full history" : `the last ${periodDays} days`
              }, openable for ${validityDays} days${
                recipient.trim() ? ` by ${recipient.trim()}` : " by the first invited recipient"
              }.`
            : "Choose an employee to see the summary."}
        </p>
        {/* Also gated on the scope list having arrived: submitting categories the
            backend never offered is exactly what the wizard exists to prevent. */}
        <button
          type="button"
          disabled={create.isPending || !subject || scope.length === 0 || !scopes.data}
          aria-busy={create.isPending}
          onClick={() => {
            setError(null);
            create.mutate();
          }}
          className="self-start rounded-xl bg-brand px-6 py-3 text-[0.9375rem] font-semibold text-brand-foreground transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
        >
          {create.isPending ? "Generating…" : "Generate KT"}
        </button>
        {error ? <FormError message={error} /> : null}
      </div>
    </section>
  );
}

function CreatedPanel({ pkg, onDone }: { pkg: KtAdmin; onDone: () => void }) {
  return (
    <section
      aria-labelledby="kt-created-heading"
      className="flex flex-col gap-4 rounded-2xl border border-brand/40 bg-brand/5 p-6 sm:p-7"
    >
      <h2 id="kt-created-heading" className="display text-lg font-semibold">
        KT created successfully
      </h2>
      <div className="flex flex-col gap-1">
        <span className="text-sm text-muted-foreground">KT ID</span>
        <span className="font-mono text-xl text-foreground">{pkg.kt_code}</span>
      </div>
      <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
        <div className="flex flex-col gap-0.5">
          <dt className="text-muted-foreground">Employee</dt>
          <dd className="text-foreground">{pkg.subject_name ?? pkg.subject_email}</dd>
        </div>
        <div className="flex flex-col gap-0.5">
          <dt className="text-muted-foreground">Knowledge scope</dt>
          <dd className="text-foreground">
            {pkg.scope.map((category) => SCOPE_LABELS[category] ?? category).join(", ")}
          </dd>
        </div>
      </dl>
      {/* What the recipient will find inside is measured at THEIR first open, under
          THEIR access — a pre-claim count here would be somebody else's visibility
          served to the caller, which is exactly what the ACL rules forbid. */}
      <p className="max-w-prose text-sm text-muted-foreground">
        Share this ID with the recipient
        {pkg.recipient_email ? ` (${pkg.recipient_email})` : ""}. They enter it under
        Knowledge Transfer in their console. It expires <When iso={pkg.expires_at} /> and
        can be revoked here at any time.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        {/* The only screen this ID appears on before it has to be passed to somebody
            else, so the confirmation has to be true: `CopyButton` awaits the clipboard
            and says "Copy failed" when the browser refused, rather than reporting
            success into a promise nobody read. */}
        <CopyButton
          value={pkg.kt_code}
          label={`Copy KT ID ${pkg.kt_code}`}
          className="border-brand/50 bg-brand px-3.5 py-2 text-sm text-brand-foreground hover:bg-brand/90 hover:text-brand-foreground"
        >
          Copy KT ID
        </CopyButton>
        <button
          type="button"
          onClick={onDone}
          className="rounded-lg border border-hairline-strong px-3.5 py-2 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        >
          Back to the list
        </button>
      </div>
    </section>
  );
}

export default function KnowledgeTransferPage() {
  const capabilities = useCapabilities();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"list" | "create">("list");
  const [created, setCreated] = useState<KtAdmin | null>(null);
  const [older, setOlder] = useState<KtAdminPage["items"]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  // Distinct from `cursor === null`, which is also the state before any walk: without
  // it the null cursor falls back to the head page's cursor and the walk restarts.
  const [exhausted, setExhausted] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // Completion is terminal, so it takes two clicks on the same row: the first turns the
  // button into the question, the second answers it. One row at a time.
  const [confirmingComplete, setConfirmingComplete] = useState<string | null>(null);
  // Revoke is terminal and has no undo, so it is armed before it fires — the same
  // two-press pattern as Complete beside it.
  const [confirmingRevoke, setConfirmingRevoke] = useState<string | null>(null);
  const [detailsId, setDetailsId] = useState<string | null>(null);

  const mayManage = can(capabilities, "kt:manage");
  const mayReadAudit = can(capabilities, "audit:read");

  const head = useQuery({
    queryKey: ["kt", "list"],
    queryFn: () => api.ktList(),
    enabled: mayManage,
  });

  const revoke = useMutation({
    mutationFn: (id: string) => api.ktRevoke(id),
    onSuccess: () => {
      toast.success("Package revoked. Its workspace stops answering immediately.");
      setOlder([]);
      setCursor(null);
      setExhausted(false);
      void queryClient.invalidateQueries({ queryKey: ["kt", "list"] });
    },
    onError: (error: unknown) => toast.error(classifyApiError(error).message),
  });

  const complete = useMutation({
    mutationFn: (id: string) => api.ktComplete(id),
    onSuccess: (_pkg, id) => {
      toast.success("Package completed. Its workspace closes; the record stays.");
      setConfirmingComplete(null);
      setOlder([]);
      setCursor(null);
      setExhausted(false);
      void queryClient.invalidateQueries({ queryKey: ["kt", "list"] });
      // An open detail panel for this package shows the old status until it refetches.
      void queryClient.invalidateQueries({ queryKey: ["kt", id] });
    },
    onError: (error: unknown) => {
      setConfirmingComplete(null);
      toast.error(classifyApiError(error).message);
    },
  });

  if (!mayManage) {
    return <PermissionDenied what="permission to manage knowledge transfer" />;
  }

  async function loadOlder() {
    const next = cursor ?? head.data?.next_cursor;
    if (!next) return;
    setLoadingMore(true);
    try {
      const page = await api.ktList({ cursor: next });
      setOlder((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
      if (page.next_cursor === null) setExhausted(true);
    } catch (error) {
      toast.error(classifyApiError(error).message);
    } finally {
      setLoadingMore(false);
    }
  }

  const rows = [...(head.data?.items ?? []), ...older];
  const more = !exhausted && (cursor ?? head.data?.next_cursor);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="Knowledge" title="Knowledge transfer">
        Create and manage controlled knowledge-transfer packages for employees leaving,
        changing roles, or onboarding new team members. A package scopes what its
        recipient sees; it never widens what they are authorised to read.
      </PageHeader>

      {created ? (
        <CreatedPanel
          pkg={created}
          onDone={() => {
            setCreated(null);
            setMode("list");
            void queryClient.invalidateQueries({ queryKey: ["kt", "list"] });
          }}
        />
      ) : mode === "create" ? (
        <>
          <CreateWizard onCreated={(pkg) => setCreated(pkg)} />
          <button
            type="button"
            onClick={() => setMode("list")}
            className="self-start rounded text-sm text-muted-foreground underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            Cancel
          </button>
        </>
      ) : (
        <>
          <button
            type="button"
            onClick={() => setMode("create")}
            className="self-start rounded-lg bg-brand px-3.5 py-2 text-sm font-medium text-brand-foreground transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            + Create KT
          </button>

          {head.error ? (
            <FailureState
              failure={classifyApiError(head.error)}
              onRetry={() => void head.refetch()}
              deniedWhat="managing knowledge transfer"
            />
          ) : head.isPending ? (
            <LoadingRegion label="Loading knowledge-transfer packages.">
              <div className="flex flex-col gap-2">
                {[0, 1, 2].map((i) => (
                  <Skeleton key={i} className="h-12" />
                ))}
              </div>
            </LoadingRegion>
          ) : rows.length === 0 ? (
            <EmptyState title="No packages yet">
              <p>
                Create one for an employee who is leaving, changing roles, or handing
                over to someone new. The recipient opens it with its KT ID.
              </p>
            </EmptyState>
          ) : (
            <>
              <TableShell
                caption="Knowledge-transfer packages with employee, KT ID, status, recipient, expiry and last activity."
                headings={[
                  "Employee",
                  "KT ID",
                  "Status",
                  "Recipient",
                  "Created",
                  "Expires",
                  "Last activity",
                  "Actions",
                ]}
                minWidth="min-w-[72rem]"
              >
                {rows.map((pkg) => (
                  <tr key={pkg.id} className="border-b border-hairline last:border-b-0">
                    <th scope="row" className="px-5 py-3.5 text-left font-normal text-foreground">
                      {pkg.subject_name ?? pkg.subject_email}
                    </th>
                    <td className="px-5 py-3.5 font-mono text-xs text-muted-foreground">
                      {pkg.kt_code}
                    </td>
                    <td className="px-5 py-3.5">
                      <Pill tone={STATUS_TONE[pkg.status] ?? "neutral"}>{pkg.status}</Pill>
                    </td>
                    <td className="px-5 py-3.5 text-xs text-muted-foreground">
                      {recipientLabel(pkg)}
                    </td>
                    <td className="px-5 py-3.5 text-xs text-muted-foreground">
                      <When iso={pkg.created_at} />
                    </td>
                    <td className="px-5 py-3.5 text-xs text-muted-foreground">
                      <When iso={pkg.expires_at} />
                    </td>
                    <td className="px-5 py-3.5 text-xs text-muted-foreground">
                      <When iso={pkg.last_activity_at} />
                    </td>
                    <td className="px-5 py-3.5">
                      <div className="flex flex-wrap gap-2">
                        <CopyButton
                          value={pkg.kt_code}
                          label={`Copy ID ${pkg.kt_code}`}
                          className="px-2.5 py-1"
                        >
                          Copy ID
                        </CopyButton>
                        <button
                          type="button"
                          aria-label={`Details for ${pkg.kt_code}`}
                          aria-expanded={detailsId === pkg.id}
                          aria-controls={detailsId === pkg.id ? "kt-details" : undefined}
                          onClick={() => setDetailsId(detailsId === pkg.id ? null : pkg.id)}
                          className={`rounded-md border px-2.5 py-1 text-xs transition-colors hover:border-brand/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand ${
                            detailsId === pkg.id
                              ? "border-brand/40 bg-brand/8 text-foreground"
                              : "border-hairline-strong"
                          }`}
                        >
                          Details
                        </button>
                        {pkg.status === "active" || pkg.status === "claimed" ? (
                          <>
                            <button
                              type="button"
                              aria-label={
                                confirmingComplete === pkg.id
                                  ? `Confirm completing ${pkg.kt_code}`
                                  : `Complete ${pkg.kt_code}`
                              }
                              disabled={complete.isPending}
                              aria-busy={complete.isPending && complete.variables === pkg.id}
                              onClick={() => {
                                if (confirmingComplete === pkg.id) {
                                  complete.mutate(pkg.id);
                                } else {
                                  setConfirmingComplete(pkg.id);
                                  setConfirmingRevoke(null);
                                }
                              }}
                              className={`rounded-md border px-2.5 py-1 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60 ${
                                confirmingComplete === pkg.id
                                  ? "border-brand/40 bg-brand/8 font-medium text-foreground"
                                  : "border-hairline-strong text-muted-foreground hover:border-brand/40 hover:text-foreground"
                              }`}
                            >
                              {confirmingComplete === pkg.id ? "Confirm complete?" : "Complete"}
                            </button>
                            {/* Two presses, matching Complete beside it — and Revoke is
                                the one that needed it more. Completing a handover is the
                                intended end of one; revoking it takes the recipient's
                                access away permanently, mid-flight, and there is no undo.
                                Shipping the gentler action behind a confirmation and the
                                harsher one on a single click was backwards. */}
                            <button
                              type="button"
                              aria-label={
                                confirmingRevoke === pkg.id
                                  ? `Confirm revoking ${pkg.kt_code}`
                                  : `Revoke ${pkg.kt_code}`
                              }
                              disabled={revoke.isPending}
                              aria-busy={revoke.isPending && revoke.variables === pkg.id}
                              onClick={() => {
                                if (confirmingRevoke === pkg.id) {
                                  revoke.mutate(pkg.id);
                                } else {
                                  setConfirmingRevoke(pkg.id);
                                  // Only one action can be armed at a time, or a second
                                  // click lands on whichever the reader forgot about.
                                  setConfirmingComplete(null);
                                }
                              }}
                              className={`rounded-md border px-2.5 py-1 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60 ${
                                confirmingRevoke === pkg.id
                                  ? "border-destructive/40 bg-destructive/8 font-medium text-destructive"
                                  : "border-hairline-strong text-muted-foreground hover:border-destructive/40 hover:text-destructive"
                              }`}
                            >
                              {confirmingRevoke === pkg.id ? "Confirm revoke?" : "Revoke"}
                            </button>
                          </>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </TableShell>
              {more ? <LoadMore onClick={() => void loadOlder()} pending={loadingMore} /> : null}
              {detailsId ? (
                <PackageDetails
                  id={detailsId}
                  canReadAudit={mayReadAudit}
                  onClose={() => setDetailsId(null)}
                  onChanged={() => {
                    setOlder([]);
                    setCursor(null);
                    setExhausted(false);
                    void queryClient.invalidateQueries({ queryKey: ["kt", "list"] });
                  }}
                />
              ) : null}
            </>
          )}
        </>
      )}
    </div>
  );
}
