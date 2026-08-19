"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Field } from "@/components/pilot/field";
import { FormShell } from "@/components/pilot/form-shell";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import { ApiError, api } from "@/lib/api";

/**
 * Employee sign-in.
 *
 * A JUTSU ID alone is never enough. It is a public-ish identifier — it appears in admin
 * screens and gets read out over the phone — so treating it as a credential would make
 * every one of those places a disclosure. The ID identifies; the emailed code
 * authenticates.
 *
 * The ID is normalised as the user types, applying Crockford's decode map so a
 * transcribed `O` becomes `0` and `I` or `l` become `1`. That is the entire reason the
 * alphabet excludes those characters, and doing it here means a correct-but-mistyped ID
 * succeeds instead of producing a "not found" the person cannot debug.
 */

/** Mirrors `normalise_jutsu_id` in packages/core. Kept deliberately small. */
function normaliseJutsuId(raw: string): string {
  const upper = raw.trim().toUpperCase().replace(/\s+/g, "");
  const parts = upper.split("-");
  if (parts.length !== 3) return upper;
  const [prefix, kind, suffix] = parts;
  return `${prefix}-${kind}-${suffix.replace(/[IL]/g, "1").replace(/O/g, "0")}`;
}

export default function EmployeeSignInPage() {
  const router = useRouter();
  const [jutsuId, setJutsuId] = useState("");
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
      // The challenge is keyed on the address, which is what the person has to control.
      // The JUTSU ID is carried through so the next step can show which account is being
      // opened; it grants nothing on its own.
      await api.requestChallenge({ email });
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
      eyebrow="Employee"
      title="Sign in to your organisation"
      lead="Enter the JUTSU ID your administrator issued you, along with your work email. We'll send a code to that address."
      backHref="/pilot"
      backLabel="Back to account type"
    >
      <form onSubmit={onSubmit} className="flex flex-col gap-5">
        <Field
          id="jutsu_id"
          name="jutsu_id"
          label="JUTSU ID"
          hint="Looks like JUTSU-EMP-4P9K2MZR. Letters that could be mistaken for digits are corrected automatically."
          value={jutsuId}
          onChange={(event) => setJutsuId(event.target.value)}
          onBlur={(event) => setJutsuId(normaliseJutsuId(event.target.value))}
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          required
          maxLength={24}
          placeholder="JUTSU-EMP-XXXXXXXX"
          className="font-mono"
        />

        <Field
          id="work_email"
          name="work_email"
          type="email"
          label="Work email"
          hint="Must be the address your organisation invited. The code goes here."
          autoComplete="work email"
          required
          maxLength={320}
        />

        {failure ? (
          <FormError message={failure.message} requestId={failure.requestId} />
        ) : null}

        <SubmitButton pending={pending} pendingLabel="Sending your code…">
          Send me a code
        </SubmitButton>

        <p className="text-xs leading-relaxed text-muted-foreground">
          No JUTSU ID yet? Your organisation&rsquo;s administrator issues one when they add
          you. Ask them to send an invitation.
        </p>
      </form>
    </FormShell>
  );
}
