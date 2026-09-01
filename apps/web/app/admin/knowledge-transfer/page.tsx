"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { useCapabilities } from "@/components/admin/admin-shell";
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
import { api, type KtAdmin, type KtAdminPage } from "@/lib/api";
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
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard.writeText(pkg.kt_code);
            toast.success("KT ID copied.");
          }}
          className="rounded-lg bg-brand px-3.5 py-2 text-sm font-medium text-brand-foreground transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        >
          Copy KT ID
        </button>
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

  const mayManage = can(capabilities, "kt:manage");

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
                caption="Knowledge-transfer packages with employee, KT ID, status, recipient and expiry."
                headings={["Employee", "KT ID", "Status", "Recipient", "Created", "Expires", "Actions"]}
                minWidth="min-w-[56rem]"
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
                      {pkg.recipient_email ?? (pkg.claimed_at ? "Claimed" : "First opener")}
                    </td>
                    <td className="px-5 py-3.5 text-xs text-muted-foreground">
                      <When iso={pkg.created_at} />
                    </td>
                    <td className="px-5 py-3.5 text-xs text-muted-foreground">
                      <When iso={pkg.expires_at} />
                    </td>
                    <td className="px-5 py-3.5">
                      <div className="flex gap-2">
                        <button
                          type="button"
                          onClick={() => {
                            void navigator.clipboard.writeText(pkg.kt_code);
                            toast.success("KT ID copied.");
                          }}
                          className="rounded-md border border-hairline-strong px-2.5 py-1 text-xs transition-colors hover:border-brand/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                        >
                          Copy ID
                        </button>
                        {pkg.status === "active" || pkg.status === "claimed" ? (
                          <button
                            type="button"
                            disabled={revoke.isPending}
                            onClick={() => revoke.mutate(pkg.id)}
                            className="rounded-md border border-hairline-strong px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-destructive/40 hover:text-destructive focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                          >
                            Revoke
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </TableShell>
              {more ? <LoadMore onClick={() => void loadOlder()} pending={loadingMore} /> : null}
            </>
          )}
        </>
      )}
    </div>
  );
}
