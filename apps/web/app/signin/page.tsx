"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Field } from "@/components/pilot/field";
import { FormShell } from "@/components/pilot/form-shell";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import { ApiError, api } from "@/lib/api";

/**
 * Signing back in.
 *
 * **A JUTSU ID alone is never enough.** It is a public-ish identifier — it appears in
 * admin screens, is printed on onboarding material and gets read out over the phone — so
 * treating it as a credential would turn every one of those places into a disclosure.
 * The ID identifies; the emailed code authenticates. That is why this asks for the work
 * email too, and why the code goes to the address rather than to whoever typed the ID.
 *
 * **Role-neutral on purpose.** An owner, an administrator and a member all do exactly
 * this, and where they land afterwards is the API's decision — `destination_for(role)`
 * returns `/admin` or `/me` and the verify screen follows it. Asking here which sort of
 * person you are would add a question whose answer the server already knows, and create
 * a second place for that mapping to be wrong. It was wrong in three places once.
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

export default function SignInPage() {
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

    // Captured before the await: React nulls `event.currentTarget` once the handler
    // yields, and reading it afterwards throws into the catch below — which would
    // report a code that was sent successfully as a failure.
    const form = new FormData(event.currentTarget);
    const email = String(form.get("work_email") ?? "");

    try {
      // The pair is what gets verified: the server delivers a code only when the JUTSU
      // ID and the address resolve to the same membership, and answers identically
      // either way so neither field becomes an oracle. The code going to the mailbox
      // stays the thing that authenticates.
      await api.requestChallenge({ email, jutsu_id: jutsuId || null });
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
      eyebrow="Console"
      title="Sign in to your console"
      lead="Enter the JUTSU ID your organisation issued you, along with your work email. We'll send a code to that address — there is no password to remember."
      backHref="/"
      backLabel="Back to home"
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
          hint="The address your organisation holds for you. The code goes here."
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
          you. Ask them to send an invitation — or{" "}
          <a
            href="/pilot/admin"
            className="text-foreground underline underline-offset-4 hover:text-brand"
          >
            register a new organisation
          </a>{" "}
          if you are setting JUTSU up.
        </p>
      </form>
    </FormShell>
  );
}
