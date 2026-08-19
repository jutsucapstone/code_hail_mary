"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { Field } from "@/components/pilot/field";
import { FormShell } from "@/components/pilot/form-shell";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import { ApiError, api } from "@/lib/api";

/**
 * Code entry — the step that actually authenticates.
 *
 * **The magic link is consumed here by a POST, never by the GET that opened it.** Mail
 * scanners, link previewers and corporate security proxies fetch every URL in a message.
 * If arriving at this page redeemed the challenge, a scanner would burn it before the
 * recipient clicked — and a redeemed link sitting in a scanner's logs is a credential
 * somebody else already used. So the token is read from the query string and submitted.
 *
 * One input, not six boxes. Six single-character fields break paste, fight password
 * managers and `autocomplete="one-time-code"`, and are a well-known screen-reader
 * nuisance for the sake of looking modern.
 */

function VerifyForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(
    null,
  );

  // Both arrive in the URL: `token` from the emailed link, `to` from the previous step so
  // this page can say where the code went. Neither is trusted — the server decides.
  const token = params.get("token") ?? "";
  const sentTo = params.get("to");

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setFailure(null);

    const form = new FormData(event.currentTarget);

    try {
      const result = await api.verify({
        token: String(form.get("token") ?? ""),
        code: String(form.get("code") ?? ""),
      });

      // The destination comes from the server. A `next` parameter honoured here would be
      // an open redirect with a freshly minted session attached, so the server chooses
      // and this only follows.
      router.push(result.destination);
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
      eyebrow="Check your email"
      title="Enter your code"
      lead={
        sentTo
          ? `We sent a six-digit code to ${sentTo}. It expires in ten minutes and can be used once.`
          : "We sent you a six-digit code. It expires in ten minutes and can be used once."
      }
      backHref="/pilot"
      backLabel="Start again"
    >
      <form onSubmit={onSubmit} className="flex flex-col gap-5">
        {/* Present when the person followed the emailed link, empty when they typed the
            code manually. Editable rather than hidden so a paste of the whole link still
            works, and so the field is not a silent, unexplained requirement. */}
        <Field
          id="token"
          name="token"
          label="Sign-in token"
          hint="Filled in automatically if you opened the link from your email."
          defaultValue={token}
          required
          minLength={16}
          maxLength={128}
          className="font-mono"
        />

        <Field
          id="code"
          name="code"
          label="Six-digit code"
          inputMode="numeric"
          pattern="[0-9]{6}"
          autoComplete="one-time-code"
          autoFocus
          required
          minLength={6}
          maxLength={6}
          placeholder="000000"
          className="font-mono"
        />

        {failure ? (
          <FormError message={failure.message} requestId={failure.requestId} />
        ) : null}

        <SubmitButton pending={pending} pendingLabel="Checking your code…">
          Continue
        </SubmitButton>

        <p className="text-xs leading-relaxed text-muted-foreground">
          Codes expire after ten minutes and allow five attempts. If you run out, request a
          new one from the start of the flow.
        </p>
      </form>
    </FormShell>
  );
}

/**
 * `useSearchParams` opts the subtree into client rendering, so it needs a Suspense
 * boundary or the whole route deopts. The fallback mirrors the real layout rather than
 * showing a spinner over a blank page (§16).
 */
export default function VerifyPage() {
  return (
    <Suspense
      fallback={
        <FormShell
          eyebrow="Check your email"
          title="Enter your code"
          backHref="/pilot"
          backLabel="Start again"
        >
          <div aria-hidden="true" className="flex flex-col gap-5">
            <div className="h-20 rounded-xl border border-hairline bg-surface/40" />
            <div className="h-20 rounded-xl border border-hairline bg-surface/40" />
            <div className="h-12 rounded-xl bg-surface/40" />
          </div>
          <p className="sr-only">Loading the verification form.</p>
        </FormShell>
      }
    >
      <VerifyForm />
    </Suspense>
  );
}
