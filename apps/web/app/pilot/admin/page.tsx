"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Field } from "@/components/pilot/field";
import { FormShell } from "@/components/pilot/form-shell";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import { ApiError, api } from "@/lib/api";

/**
 * Organisation registration — the first step of the admin path.
 *
 * A client component because it submits and handles failure. Everything it renders on
 * first paint is static, so the interactive cost is limited to this one form rather than
 * the page around it.
 *
 * Validation is HTML-native (`required`, `type="email"`) plus whatever the API says. There
 * is no second rulebook in the browser: the API already returns a typed
 * `validation_failed` envelope, and a client-side copy would be a second source of truth
 * that disagrees the moment either side changes — with the browser's copy being the one
 * that cannot be enforced.
 */

const ORG_SIZES = ["1-10", "11-50", "51-200", "201-1000", "1000+"] as const;

export default function AdminRegistrationPage() {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(
    null,
  );

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setFailure(null);

    const form = new FormData(event.currentTarget);
    const email = String(form.get("work_email") ?? "");

    try {
      await api.registerOrganisation({
        full_name: String(form.get("full_name") ?? ""),
        work_email: email,
        company_name: String(form.get("company_name") ?? ""),
        company_domain: String(form.get("company_domain") ?? ""),
        job_title: String(form.get("job_title") ?? ""),
        org_size: String(form.get("org_size") ?? ""),
      });

      // The address is carried forward so the next screen can say who the code went to.
      // It is not a credential and it is not trusted — verification is keyed on the
      // token and code that only reached the inbox.
      router.push(`/pilot/verify?to=${encodeURIComponent(email)}`);
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : "Something went wrong. Please try again.";
      setFailure({
        message,
        requestId: error instanceof ApiError ? error.requestId : undefined,
      });
      setPending(false);
    }
  }

  return (
    <FormShell
      eyebrow="Admin / HR"
      title="Set up your organisation"
      lead="You'll be the first administrator. We'll email you a code to confirm this address — no password to choose or remember."
      backHref="/pilot"
      backLabel="Back to account type"
      size="wide"
    >
      {/* Two columns from `sm` up, so six fields occupy three rows and the submit button
          stays above the fold on a laptop. Stacked below that, where a second column
          would leave each field too narrow to read its own hint.

          `items-end` is what keeps the rows tidy: only some fields carry a hint, so a
          stretched row puts one input 47px lower than its neighbour. Aligning to the
          bottom lets the labels sit at different heights while the inputs line up, which
          is the edge the eye actually follows down a form. */}
      <form
        onSubmit={onSubmit}
        noValidate={false}
        className="grid items-end gap-x-6 gap-y-4 sm:grid-cols-2"
      >
        <Field
          id="full_name"
          name="full_name"
          label="Full name"
          autoComplete="name"
          required
          maxLength={255}
        />

        <Field
          id="work_email"
          name="work_email"
          type="email"
          label="Work email"
          hint="Use your organisation's domain. The confirmation code goes here."
          autoComplete="work email"
          required
          maxLength={320}
        />

        <Field
          id="company_name"
          name="company_name"
          label="Organisation name"
          autoComplete="organization"
          required
          maxLength={255}
        />

        <Field
          id="company_domain"
          name="company_domain"
          label="Organisation domain"
          hint="For example, example.com — used to recognise colleagues who join later."
          required
          minLength={3}
          maxLength={255}
          placeholder="example.com"
        />

        <Field
          id="job_title"
          name="job_title"
          label="Job title"
          autoComplete="organization-title"
          required
          maxLength={128}
        />

        <div className="flex flex-col gap-2">
          <label htmlFor="org_size" className="text-sm font-medium text-foreground">
            Organisation size
          </label>
          <select
            id="org_size"
            name="org_size"
            required
            defaultValue=""
            className="h-11 rounded-xl border border-hairline-strong bg-surface/40 px-3.5 text-sm text-foreground transition-colors duration-200 hover:border-hairline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            <option value="" disabled>
              Select a range
            </option>
            {ORG_SIZES.map((size) => (
              <option key={size} value={size}>
                {size} people
              </option>
            ))}
          </select>
        </div>

        {failure ? (
          <div className="sm:col-span-2">
            <FormError message={failure.message} requestId={failure.requestId} />
          </div>
        ) : null}

        <div className="sm:col-span-2">
          <SubmitButton pending={pending} pendingLabel="Creating your organisation…">
            Create organisation
          </SubmitButton>
        </div>

        <p className="text-xs leading-relaxed text-muted-foreground sm:col-span-2">
          JUTSU connects to your tools read-only. Nothing is connected until you choose it,
          and nothing is ever written back.
        </p>
      </form>
    </FormShell>
  );
}
