"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState, useSyncExternalStore } from "react";
import { ArrowLeft } from "lucide-react";

import { Field } from "@/components/pilot/field";
import { FormShell } from "@/components/pilot/form-shell";
import { SelectField } from "@/components/pilot/select-field";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import { ApiError, api } from "@/lib/api";
import type { components } from "@/lib/api-schema";
import { INDUSTRIES, countryOptions } from "@/lib/onboarding";

type RegisterBody = components["schemas"]["RegisterPayload"];

/**
 * The one error code this screen routes to a specific field.
 *
 * Matched on the stable `code`, never on the message — the prose is user-facing copy and
 * is expected to be reworded, and a comparison against it would break silently on the
 * day someone does, degrading to a form-level box rather than an error anyone notices.
 */
const DOMAIN_MISMATCH = "domain_mismatch";

/**
 * Organisation registration — the first step of the admin path.
 *
 * **Two panes, one route, one submission.** The form outgrew a single screen: nine
 * controls in the previous two-column layout needed roughly 846px of card, and the
 * shell fits in 718px today — measured, and a 1280x720 laptop is an ordinary machine.
 * Rather than let the submit button fall below the fold, the organisation and the person
 * are asked for separately.
 *
 * The split is presentational only. Pane one never touches the network, nothing is
 * persisted between panes, and the single POST happens at the end — so there is no
 * partial organisation to clean up if someone abandons this halfway, and no server state
 * keyed to a browser that may never come back.
 *
 * State lives in React rather than in the URL or in storage. A refresh clears it, which
 * is the honest behaviour: the alternative is a half-filled form restored from
 * `sessionStorage` that disagrees with what the server will accept.
 *
 * Validation is HTML-native (`required`, `type="email"`) plus whatever the API says. There
 * is no second rulebook in the browser: the API already returns a typed
 * `validation_failed` envelope, and a client-side copy would be a second source of truth
 * that disagrees the moment either side changes — with the browser's copy being the one
 * that cannot be enforced.
 */

const ORG_SIZES = ["1-10", "11-50", "51-200", "201-1000", "1000+"] as const;

/** Never resubscribes — the value flips once, at hydration, and stays. */
const subscribeNever = () => () => {};

/** What pane one collects. Held in memory until pane two submits. */
interface OrganisationDetails {
  company_name: string;
  company_domain: string;
  org_size: string;
  country: string;
  industry: string;
}

export default function AdminRegistrationPage() {
  const router = useRouter();
  // Which pane is showing, and what has been collected, are separate pieces of state on
  // purpose. Deriving the pane from `details !== null` meant going back had to discard
  // the details to get there — and the fields then came back empty. Measured: the text
  // inputs happened to keep their values because React reused the DOM nodes, while all
  // three selects reset, silently losing the *required* organisation size. Form state
  // that survives only by reconciliation luck is not state that survives.
  const [pane, setPane] = useState<1 | 2>(1);
  const [details, setDetails] = useState<OrganisationDetails | null>(null);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(
    null,
  );
  // Held apart from `failure` so a field-level rejection renders on the field.
  const [emailError, setEmailError] = useState<string | null>(null);

  // Client-only, and memoised so it is not recomputed on every keystroke.
  //
  // `countryOptions` reads the environment's locale and ICU data, which differ between
  // Node and the browser — the server rendered one order and the browser another, and
  // every load failed hydration. Rendering just the placeholder until mounted keeps the
  // markup identical on both sides. The field is optional and the list appears the
  // moment the page is interactive, so nothing is lost.
  const mounted = useSyncExternalStore(subscribeNever, () => true, () => false);
  const countries = useMemo(() => (mounted ? countryOptions() : []), [mounted]);

  function onOrganisationSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    // Trimmed on capture, not on send. The API strips whitespace anyway, but these
    // values are also rendered back on the next pane — an untrimmed entry produced
    // "Must be at muj.com ." and "administrator of manipal university jaipur .", which
    // reads as a typo in our copy rather than in what was typed.
    const trimmed = (field: string) => String(form.get(field) ?? "").trim();
    setDetails({
      company_name: trimmed("company_name"),
      // Lower-cased here as well as on the server. `canonical_domain` will store it this
      // way regardless, so echoing back the capitals someone happened to type — "Must be
      // at DEMO.COM" — promises a domain that is not the one being created.
      company_domain: trimmed("company_domain").toLowerCase(),
      org_size: trimmed("org_size"),
      country: trimmed("country"),
      industry: trimmed("industry"),
    });
    setPane(2);
    setFailure(null);
  }

  async function onAdministratorSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!details) return;

    setPending(true);
    setFailure(null);

    // Captured before the await. React nulls `event.currentTarget` once the handler
    // yields, so reading it afterwards throws — into the catch below, which would report
    // a successful registration as a failure.
    const form = new FormData(event.currentTarget);
    const email = String(form.get("work_email") ?? "");

    const body: RegisterBody = {
      full_name: String(form.get("full_name") ?? ""),
      work_email: email,
      job_title: String(form.get("job_title") ?? ""),
      company_name: details.company_name,
      company_domain: details.company_domain,
      org_size: details.org_size,
      // Optional: send null rather than "" so the API's `country: str | None` sees an
      // absence instead of a value that fails its pattern.
      country: details.country || null,
      industry: (details.industry || null) as RegisterBody["industry"],
      // The server holds the document versions. A version chosen here would let a stale
      // cached bundle name a document nobody published.
      terms_accepted: true,
    };

    try {
      await api.registerOrganisation(body);
      // Carried forward so the next screen can say who the code went to. It is not a
      // credential and it is not trusted — completion is keyed on the token and code
      // that only reached the inbox. `flow` tells that screen which endpoint completes
      // this: a registration code cannot open a session, and vice versa.
      router.push(`/pilot/verify?flow=register&to=${encodeURIComponent(email)}`);
    } catch (error) {
      // `domain_mismatch` is about one field, so it is shown on that field rather than
      // in the form-level box at the bottom. The box is correct for failures that belong
      // to the submission as a whole — it is the wrong place for "this input is wrong",
      // because the reader has to carry the sentence back up the form and work out which
      // of three inputs it meant.
      //
      // Still the server's error, not a rule restated here. The API stays the only
      // authority on what a valid pairing is; this only decides where its answer lands.
      if (error instanceof ApiError && error.code === DOMAIN_MISMATCH) {
        setEmailError(error.message);
        setFailure(null);
        setPending(false);
        return;
      }

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

  // `|| !details` is not defensive noise: it makes "pane two without collected details"
  // unrepresentable rather than merely unlikely, and it is what tells TypeScript the
  // second pane can read `details` without a non-null assertion.
  if (pane === 1 || !details) {
    return (
      <FormShell
        eyebrow="Admin / HR · Step 1 of 2"
        title="About your organisation"
        lead="We use the domain to recognise colleagues who join later, and to confirm you work there."
        backHref="/pilot"
        backLabel="Back to account type"
        size="wide"
      >
        {/* Two columns from `sm` up. `items-end` keeps the inputs on one line while the
            labels sit at different heights — only some fields carry a hint, and a
            stretched row would drop one input 47px below its neighbour. */}
        {/* Keyed so the two panes never reconcile into one another.

            Without it React reuses the DOM nodes — same component, same position in the
            tree — and pane one's first two inputs become pane two's. Measured: the
            administrator's "Full name" arrived pre-filled with the company name and
            "Work email" with the bare domain. A distinct key forces a remount, which is
            what makes each pane's fields actually its own. */}
        <form
          key="organisation"
          onSubmit={onOrganisationSubmit}
          className="grid items-end gap-x-6 gap-y-4 sm:grid-cols-2"
        >
          <Field
            id="company_name"
            name="company_name"
            label="Organisation name"
            autoComplete="organization"
            required
            maxLength={255}
            defaultValue={details?.company_name ?? ""}
          />

          <Field
            id="company_domain"
            name="company_domain"
            label="Organisation domain"
            hint="For example, example.com — your work email must be at this domain."
            required
            minLength={3}
            maxLength={255}
            placeholder="example.com"
            defaultValue={details?.company_domain ?? ""}
          />

          <SelectField
            id="org_size"
            name="org_size"
            label="Organisation size"
            placeholder="Select a range"
            required
            defaultValue={details?.org_size ?? ""}
          >
            {ORG_SIZES.map((size) => (
              <option key={size} value={size}>
                {size} people
              </option>
            ))}
          </SelectField>

          <SelectField
            id="industry"
            name="industry"
            label="Industry (optional)"
            placeholder="Select an industry"
            defaultValue={details?.industry ?? ""}
          >
            {INDUSTRIES.map((industry) => (
              <option key={industry.value} value={industry.value}>
                {industry.label}
              </option>
            ))}
          </SelectField>

          <SelectField
            id="country"
            name="country"
            label="Country or region (optional)"
            placeholder="Select a country"
            className="sm:col-span-2"
            defaultValue={details?.country ?? ""}
          >
            {countries.map((country) => (
              <option key={country.value} value={country.value}>
                {country.label}
              </option>
            ))}
          </SelectField>

          <div className="sm:col-span-2">
            {/* Never pending: this pane does no network work, it only advances. */}
            <SubmitButton pending={false} pendingLabel="Continue">
              Continue
            </SubmitButton>
          </div>
        </form>
      </FormShell>
    );
  }

  return (
    <FormShell
      eyebrow="Admin / HR · Step 2 of 2"
      title="About you"
      lead={`You'll be the first administrator of ${details.company_name}. We'll email a code to confirm your address — no password to choose or remember.`}
      backHref="/pilot"
      backLabel="Back to account type"
      size="wide"
    >
      <form
        key="administrator"
        onSubmit={onAdministratorSubmit}
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
          hint={`Must be at ${details.company_domain}. The confirmation code goes here.`}
          autoComplete="work email"
          required
          maxLength={320}
          error={emailError ?? undefined}
          // Cleared on edit rather than left until the next submit. A red field beside
          // an address the reader has already corrected says the correction did not
          // take, and the usual next move is to change something that was right.
          onChange={() => setEmailError(null)}
        />

        <Field
          id="job_title"
          name="job_title"
          label="Job title"
          autoComplete="organization-title"
          required
          maxLength={128}
          className="sm:col-span-2"
        />

        {/* Required, and enforced server-side as `Literal[True]` — an unticked box is a
            422 rather than a quietly stored `false`. The version accepted is a server
            constant, recorded with the moment of consent. */}
        <div className="flex items-start gap-3 sm:col-span-2">
          <input
            id="terms_accepted"
            name="terms_accepted"
            type="checkbox"
            required
            className="mt-0.5 size-4 shrink-0 rounded border-hairline-strong accent-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          />
          <label htmlFor="terms_accepted" className="text-sm leading-relaxed text-muted-foreground">
            I agree to the{" "}
            <a
              href="/terms"
              target="_blank"
              rel="noreferrer"
              className="text-foreground underline underline-offset-4 hover:text-brand"
            >
              Terms of Service
            </a>{" "}
            and{" "}
            <a
              href="/privacy"
              target="_blank"
              rel="noreferrer"
              className="text-foreground underline underline-offset-4 hover:text-brand"
            >
              Privacy Policy
            </a>
            .
          </label>
        </div>

        {failure ? (
          <div className="sm:col-span-2">
            <FormError message={failure.message} requestId={failure.requestId} />
          </div>
        ) : null}

        <div className="flex flex-col gap-3 sm:col-span-2 sm:flex-row-reverse sm:items-center">
          <SubmitButton
            pending={pending}
            pendingLabel="Sending your code…"
            className="sm:flex-1"
          >
            Create organisation
          </SubmitButton>
          {/* Returns to pane one with everything still in memory. A browser Back would
              leave the route and lose it, so the way back is an explicit control. */}
          <button
            type="button"
            onClick={() => setPane(1)}
            className="inline-flex items-center justify-center gap-2 rounded-xl border border-hairline-strong px-4 py-3 text-sm text-muted-foreground transition-colors hover:border-brand/40 hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            <ArrowLeft aria-hidden="true" className="size-4" />
            Organisation details
          </button>
        </div>

        <p className="text-xs leading-relaxed text-muted-foreground sm:col-span-2">
          JUTSU connects to your tools read-only. Nothing is connected until you choose it,
          and nothing is ever written back.
        </p>
      </form>
    </FormShell>
  );
}
