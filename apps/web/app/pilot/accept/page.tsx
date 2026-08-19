"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { Field } from "@/components/pilot/field";
import { FormShell } from "@/components/pilot/form-shell";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import { ApiError, api } from "@/lib/api";

/**
 * Accepting an invitation.
 *
 * Holding the token already proves control of the invited address — it reached that
 * inbox and nowhere else — so there is no second code to enter. The invitation *is* the
 * challenge, and accepting signs the person in.
 *
 * It is consumed by a POST from this page rather than by the GET that opened the link,
 * for the same reason the magic link is: mail scanners, link previewers and corporate
 * security proxies fetch every URL in a message. A GET that accepted would let a scanner
 * create the account and burn the invitation before the recipient ever clicked.
 */

function AcceptForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(
    null,
  );
  const [issued, setIssued] = useState<string | null>(null);

  const token = params.get("token") ?? "";

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setFailure(null);

    const form = new FormData(event.currentTarget);

    try {
      const result = await api.acceptInvitation({
        token: String(form.get("token") ?? ""),
        full_name: String(form.get("full_name") ?? ""),
      });

      // Shown before navigating: this is the only moment the person sees their JUTSU ID
      // in context, and they will be asked for it the next time they sign in.
      setIssued(result.jutsu_id);
      setTimeout(() => router.push(result.destination), 2500);
    } catch (error) {
      setFailure({
        message:
          error instanceof ApiError ? error.message : "Something went wrong. Please try again.",
        requestId: error instanceof ApiError ? error.requestId : undefined,
      });
      setPending(false);
    }
  }

  if (issued) {
    return (
      <FormShell
        eyebrow="Welcome"
        title="You're in"
        lead="Your account is ready. This is your JUTSU ID — you'll be asked for it when you sign in."
        backHref="/pilot"
        backLabel="Back to start"
      >
        <div role="status" aria-live="polite" className="flex flex-col gap-4">
          <p className="rounded-xl border border-brand/40 bg-brand/8 px-4 py-4 text-center font-mono text-lg tracking-[0.12em] text-foreground">
            {issued}
          </p>
          <p className="text-sm leading-relaxed text-muted-foreground">
            Taking you to your profile. You can find this ID again in your settings at any
            time.
          </p>
        </div>
      </FormShell>
    );
  }

  return (
    <FormShell
      eyebrow="Invitation"
      title="Join your organisation"
      lead="Your organisation invited you to JUTSU. Confirm your name and we'll issue your JUTSU ID."
      backHref="/pilot"
      backLabel="Back to start"
    >
      <form onSubmit={onSubmit} className="flex flex-col gap-5">
        <Field
          id="full_name"
          name="full_name"
          label="Full name"
          hint="How you'll appear to colleagues."
          autoComplete="name"
          required
          maxLength={255}
        />

        {/* Prefilled from the link. Editable rather than hidden so pasting a whole link
            still works, and so the field is not an unexplained silent requirement. */}
        <Field
          id="token"
          name="token"
          label="Invitation token"
          hint="Filled in automatically if you opened the link from your email."
          defaultValue={token}
          required
          minLength={16}
          maxLength={128}
          className="font-mono"
        />

        {failure ? (
          <FormError message={failure.message} requestId={failure.requestId} />
        ) : null}

        <SubmitButton pending={pending} pendingLabel="Setting up your account…">
          Accept and continue
        </SubmitButton>

        <p className="text-xs leading-relaxed text-muted-foreground">
          Invitations expire, and each one can be used once. If yours no longer works, ask
          your administrator to send another.
        </p>
      </form>
    </FormShell>
  );
}

export default function AcceptInvitationPage() {
  return (
    <Suspense
      fallback={
        <FormShell
          eyebrow="Invitation"
          title="Join your organisation"
          backHref="/pilot"
          backLabel="Back to start"
        >
          <div aria-hidden="true" className="flex flex-col gap-5">
            <div className="h-20 rounded-xl border border-hairline bg-surface/40" />
            <div className="h-20 rounded-xl border border-hairline bg-surface/40" />
            <div className="h-12 rounded-xl bg-surface/40" />
          </div>
          <p className="sr-only">Loading the invitation form.</p>
        </FormShell>
      }
    >
      <AcceptForm />
    </Suspense>
  );
}
