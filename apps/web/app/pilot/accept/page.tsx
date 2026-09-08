"use client";

import { useRouter } from "next/navigation";
import { Suspense, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { Field } from "@/components/pilot/field";
import { FormShell } from "@/components/pilot/form-shell";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import { ApiError, api } from "@/lib/api";
import { useLinkToken } from "@/lib/link-token";

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
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(
    null,
  );
  const [issued, setIssued] = useState<string | null>(null);

  // From the URL fragment, which the browser never sends anywhere. See `useLinkToken`.
  const token = useLinkToken();

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
          {/* This is the one screen the ID appears on, and the console asks for it by
              name at every subsequent sign-in — so transcribing it by eye was the only
              way to keep it, and a closed tab cost somebody an email to their
              administrator. */}
          <div className="flex justify-center">
            <CopyButton value={issued} label={`Copy JUTSU ID ${issued}`}>
              Copy your JUTSU ID
            </CopyButton>
          </div>
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
            still works, and so the field is not an unexplained silent requirement.

            `key={token}` is load-bearing now that the token comes from the fragment: it
            arrives in an effect, one render AFTER the input mounted, and `defaultValue`
            is only read on mount. Without the key the field would stay empty for every
            person who followed the link — and stay empty in a way that looks like the
            link failed. Changing the key remounts the input once, when the value
            arrives; typing afterwards does not change `token`, so the field stays
            editable. */}
        <Field
          key={token}
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
