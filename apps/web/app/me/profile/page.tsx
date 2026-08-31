"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Check } from "lucide-react";

import { Field } from "@/components/pilot/field";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import { ErrorState, LoadingRegion, Skeleton } from "@/components/states";
import { ApiError, api, type EmployeeProfile } from "@/lib/api";

/**
 * The employee's own profile.
 *
 * Reads `GET /v1/me/profile` and writes `PATCH /v1/me/profile`. Nothing else: the server
 * takes the person from the session cookie and the organisation from the request's tenant
 * scope, so there is no identity for this page to send and no field on the form that
 * could carry one. The API rejects `user_id` and `org_id` outright rather than ignoring
 * them, and `additionalProperties: false` in the generated types means TypeScript refuses
 * them before the request is even built.
 *
 * **A 404 is the empty state, not an error.** Migration 0002 is explicit that an owner or
 * an IT admin is a user row with no profile at all, so "you have not filled this in yet"
 * is the correct reading of a missing row — and rendering it as a failure would teach
 * people that a normal state is broken.
 */

/** The seven columns `ProfilePatch` accepts. Skills is edited as comma-separated text. */
interface Draft {
  employee_code: string;
  department: string;
  designation: string;
  joining_date: string;
  phone_e164: string;
  skills: string;
  responsibilities: string;
}

const EMPTY: Draft = {
  employee_code: "",
  department: "",
  designation: "",
  joining_date: "",
  phone_e164: "",
  skills: "",
  responsibilities: "",
};

function toDraft(profile: EmployeeProfile | null): Draft {
  if (!profile) return EMPTY;
  return {
    employee_code: profile.employee_code ?? "",
    department: profile.department ?? "",
    designation: profile.designation ?? "",
    joining_date: profile.joining_date ?? "",
    phone_e164: profile.phone_e164 ?? "",
    skills: (profile.skills ?? []).join(", "),
    responsibilities: profile.responsibilities ?? "",
  };
}

/**
 * An empty text box means "clear this field", which the API expresses as `null`.
 *
 * Sending `""` instead would store an empty string — a third state alongside "unset" and
 * "has a value" that nothing else in the system understands.
 */
function blankToNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

export default function ProfilePage() {
  const [profile, setProfile] = useState<EmployeeProfile | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [missing, setMissing] = useState(false);
  const [loadFailure, setLoadFailure] = useState<{ message: string; requestId?: string } | null>(
    null,
  );
  const [saving, setSaving] = useState(false);
  const [saveFailure, setSaveFailure] = useState<{ message: string; requestId?: string } | null>(
    null,
  );
  const [saved, setSaved] = useState(false);

  // State is set inside `.then`/`.catch`, never synchronously in the effect below —
  // `react-hooks/set-state-in-effect`, same shape the other console pages use.
  const load = useCallback(() => {
    api
      .myProfile()
      .then((result) => {
        setProfile(result);
        setDraft(toDraft(result));
        setMissing(false);
        setLoadFailure(null);
        setLoading(false);
      })
      .catch((error: unknown) => {
        // 404 is "not filled in yet", which is a form to complete rather than a failure.
        if (error instanceof ApiError && error.status === 404) {
          setProfile(null);
          setDraft(EMPTY);
          setMissing(true);
          setLoadFailure(null);
        } else {
          setLoadFailure({
            message: error instanceof ApiError ? error.message : "That did not load.",
            requestId: error instanceof ApiError ? error.requestId : undefined,
          });
        }
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const pristine = useMemo(
    () => JSON.stringify(draft) === JSON.stringify(toDraft(profile)),
    [draft, profile],
  );

  const set = useCallback((field: keyof Draft, value: string) => {
    setDraft((current) => ({ ...current, [field]: value }));
    // Any edit invalidates the previous outcome — leaving "Saved" on screen while the
    // form has changed underneath it would be a lie about the stored state.
    setSaved(false);
    setSaveFailure(null);
  }, []);

  const submit = useCallback(
    (event: React.FormEvent) => {
      event.preventDefault();
      if (saving) return;
      setSaving(true);
      setSaveFailure(null);
      setSaved(false);

      // Exactly the seven ProfilePatch fields. No identity, no tenant — there is nothing
      // else this form is able to send.
      api
        .updateMyProfile({
          employee_code: blankToNull(draft.employee_code),
          department: blankToNull(draft.department),
          designation: blankToNull(draft.designation),
          joining_date: blankToNull(draft.joining_date),
          phone_e164: blankToNull(draft.phone_e164),
          skills: draft.skills
            .split(",")
            .map((skill) => skill.trim())
            .filter(Boolean),
          responsibilities: blankToNull(draft.responsibilities),
        })
        .then((result) => {
          setProfile(result);
          setDraft(toDraft(result));
          setMissing(false);
          setSaved(true);
        })
        .catch((error: unknown) => {
          setSaveFailure({
            message:
              error instanceof ApiError
                ? error.message
                : "We could not save that. Please try again.",
            requestId: error instanceof ApiError ? error.requestId : undefined,
          });
        })
        .finally(() => setSaving(false));
    },
    [draft, saving],
  );

  const cancel = useCallback(() => {
    setDraft(toDraft(profile));
    setSaveFailure(null);
    setSaved(false);
  }, [profile]);

  return (
    <div className="flex flex-col gap-8">
      <header>
        <p className="eyebrow flex items-center gap-2.5 text-brand">
          <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
          Your details
        </p>
        <h1 className="display mt-4 text-3xl font-semibold sm:text-4xl">Profile</h1>
        <p className="mt-3 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Your role and working context. This is separate from your access — changing it
          does not change what you can read.
        </p>
      </header>

      {loading ? (
        <LoadingRegion label="Loading your profile">
          <div className="space-y-3">
            {[0, 1, 2, 3].map((n) => (
              <Skeleton key={n} className="h-16 w-full" />
            ))}
          </div>
        </LoadingRegion>
      ) : loadFailure ? (
        <ErrorState
          message={loadFailure.message}
          requestId={loadFailure.requestId}
          onRetry={load}
        />
      ) : (
        <>
          {missing ? (
            <div
              className="rounded-2xl border border-hairline bg-surface/40 p-6"
              data-testid="profile-empty"
            >
              <p className="eyebrow text-muted-foreground/80">Not filled in yet</p>
              <p className="mt-3 text-pretty text-sm leading-relaxed text-muted-foreground">
                You do not have a profile yet. Complete the form below and it will be
                created — nothing is stored until you save.
              </p>
            </div>
          ) : null}

          <form onSubmit={submit} className="flex flex-col gap-6" noValidate>
            <div className="grid gap-6 sm:grid-cols-2">
              <Field
                id="employee_code"
                label="Employee code"
                value={draft.employee_code}
                maxLength={64}
                onChange={(e) => set("employee_code", e.target.value)}
              />
              <Field
                id="department"
                label="Department"
                value={draft.department}
                maxLength={128}
                onChange={(e) => set("department", e.target.value)}
              />
              <Field
                id="designation"
                label="Designation"
                value={draft.designation}
                maxLength={128}
                onChange={(e) => set("designation", e.target.value)}
              />
              <Field
                id="joining_date"
                label="Joining date"
                type="date"
                value={draft.joining_date}
                onChange={(e) => set("joining_date", e.target.value)}
              />
              <Field
                id="phone_e164"
                label="Phone"
                hint="International format, for example +919876543210."
                value={draft.phone_e164}
                maxLength={20}
                onChange={(e) => set("phone_e164", e.target.value)}
              />
              <Field
                id="skills"
                label="Skills"
                hint="Separate with commas."
                value={draft.skills}
                onChange={(e) => set("skills", e.target.value)}
              />
            </div>

            <div className="flex flex-col gap-2">
              <label htmlFor="responsibilities" className="text-sm font-medium text-foreground">
                Responsibilities
              </label>
              <textarea
                id="responsibilities"
                rows={4}
                value={draft.responsibilities}
                onChange={(e) => set("responsibilities", e.target.value)}
                className="rounded-xl border border-hairline-strong bg-surface/40 px-3.5 py-3 text-sm text-foreground transition-colors duration-200 placeholder:text-muted-foreground/70 hover:border-hairline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
              />
            </div>

            {saveFailure ? (
              <FormError message={saveFailure.message} requestId={saveFailure.requestId} />
            ) : null}

            {/* Announced, not merely coloured: a tick that appears silently tells a
                screen-reader user nothing about whether their save worked. */}
            {saved ? (
              <p
                role="status"
                data-testid="profile-saved"
                className="flex items-center gap-2 text-sm text-brand"
              >
                <Check aria-hidden="true" className="size-4" />
                Profile saved.
              </p>
            ) : null}

            <div className="flex flex-col-reverse gap-3 sm:flex-row sm:items-center">
              <button
                type="button"
                onClick={cancel}
                disabled={saving || pristine}
                className="h-12 rounded-xl border border-hairline-strong px-6 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand sm:w-auto"
              >
                Cancel
              </button>
              <SubmitButton
                pending={saving}
                pendingLabel="Saving…"
                className="sm:w-auto sm:min-w-44"
              >
                Save profile
              </SubmitButton>
            </div>
          </form>
        </>
      )}
    </div>
  );
}
