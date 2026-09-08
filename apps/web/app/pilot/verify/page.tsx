"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { CodeInput } from "@/components/pilot/code-input";
import { FormShell } from "@/components/pilot/form-shell";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import { ApiError, api } from "@/lib/api";
import { VERIFY_ADDRESS_KEY, useHandoff, useLinkToken } from "@/lib/link-token";

/**
 * Code entry — the step that actually authenticates.
 *
 * **The magic link is consumed here by a POST, never by the GET that opened it.** Mail
 * scanners, link previewers and corporate security proxies fetch every URL in a message.
 * If arriving at this page redeemed the challenge, a scanner would burn it before the
 * recipient clicked — and a redeemed link sitting in a scanner's logs is a credential
 * somebody else already used. So the token is read from the URL fragment — which the
 * browser never transmits — and submitted.
 *
 * **The token is no longer something a person has to produce.** It used to be a required
 * field, and the only place to obtain one was the emailed link — so the six-digit code,
 * the entire point of this screen, could not be used on its own: the browser refused to
 * submit on a field whose value nobody had been shown. `POST /v1/auth/request` now leaves
 * the token in an httpOnly `__Host-` cookie that expires with the challenge, so asking
 * for the code is enough. The link's token still wins when there is one, which keeps the
 * emailed link working on a device that never asked for a code.
 */

/** How long before the same address may ask for another code. */
const RESEND_SECONDS = 45;

function VerifyForm() {
  const router = useRouter();
  const params = useSearchParams();

  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(
    null,
  );
  // Remounts the code field after a rejection: a fresh, empty, focused set of boxes is
  // what a person expects to type into, and it means the failed code cannot be
  // half-edited into the next attempt.
  const [attempt, setAttempt] = useState(0);
  const [resendIn, setResendIn] = useState(0);
  const [resent, setResent] = useState(false);

  // Three inputs, and they arrive by three different routes on purpose.
  //
  // `token` comes from the URL FRAGMENT, which a browser never transmits — see
  // `useLinkToken`. In the query string it was recorded verbatim in the Cloud Run
  // request log, which is where live challenge tokens were found.
  //
  // `to` comes from `sessionStorage`, written by whichever page sent the code. It is
  // only ever used to tell the reader where to look, so it does not need to reach a
  // server at all — and as a query parameter it put a registrant's work email address
  // into that same request log.
  //
  // `flow` stays in the query string. It is not a secret, and it selects a *route*
  // rather than a permission: both endpoints assert the challenge's purpose server-side,
  // so pointing this at the wrong one yields the same refusal as a wrong code.
  //
  // None of the three is trusted. The server decides in every case.
  const token = useLinkToken();
  const sentTo = useHandoff(VERIFY_ADDRESS_KEY);
  const registering = params.get("flow") === "register";

  useEffect(() => {
    if (resendIn <= 0) return;
    const timer = setTimeout(() => setResendIn((seconds) => seconds - 1), 1000);
    return () => clearTimeout(timer);
  }, [resendIn]);

  async function submit(code: string) {
    if (pending) return;
    setPending(true);
    setFailure(null);

    try {
      // An empty token is omitted rather than sent: the server reads the cookie when the
      // body carries nothing, and sending "" would look like a supplied-but-wrong token.
      const credentials = { token: token || null, code };
      // Completing a registration is what creates the organisation — nothing exists
      // until this call succeeds.
      const result = registering
        ? await api.completeRegistration(credentials)
        : await api.verify(credentials);

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
      setAttempt((n) => n + 1);
      setPending(false);
    }
  }

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    await submit(String(form.get("code") ?? ""));
  }

  async function onResend() {
    if (!sentTo || resendIn > 0) return;
    setFailure(null);
    setResent(false);
    try {
      await api.requestChallenge({ email: sentTo, jutsu_id: null });
      // The countdown starts whatever the server said. `POST /v1/auth/request` answers
      // 202 for an address with no account as well, deliberately, so a countdown that
      // only ran on "success" would answer the question that endpoint refuses to.
      setResent(true);
      setResendIn(RESEND_SECONDS);
      setAttempt((n) => n + 1);
    } catch (error) {
      setFailure({
        message:
          error instanceof ApiError
            ? error.message
            : "We could not send another code. Please try again.",
        requestId: error instanceof ApiError ? error.requestId : undefined,
      });
      setResendIn(RESEND_SECONDS);
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
        {/* Present only when the person followed the emailed link. Hidden rather than
            editable now that the cookie covers the typed path: a visible field for a
            value nobody can produce is what made this screen impossible to complete. */}
        {token ? <input type="hidden" name="token" value={token} /> : null}

        <CodeInput
          key={attempt}
          id="code"
          name="code"
          label="Six-digit code"
          hint="Type it or paste it — the whole code lands in one go."
          autoFocus
          disabled={pending}
          invalid={Boolean(failure)}
          describedBy={failure ? "verify-error" : undefined}
          onComplete={(code) => {
            void submit(code);
          }}
        />

        {failure ? (
          <FormError
            id="verify-error"
            message={failure.message}
            requestId={failure.requestId}
          />
        ) : null}

        <SubmitButton pending={pending} pendingLabel="Checking your code…">
          Continue
        </SubmitButton>

        <div className="flex flex-col gap-2">
          {sentTo && !registering ? (
            <p className="text-xs leading-relaxed text-muted-foreground">
              {"Didn't get it? "}
              <button
                type="button"
                onClick={() => void onResend()}
                disabled={resendIn > 0 || pending}
                className="font-medium text-brand underline underline-offset-4 disabled:no-underline disabled:opacity-60"
              >
                {resendIn > 0 ? `Send a new code in ${resendIn}s` : "Send a new code"}
              </button>
            </p>
          ) : null}

          {/* Polite, not assertive: it confirms something the person asked for and must
              not interrupt them mid-code. */}
          <p aria-live="polite" className="text-xs leading-relaxed text-muted-foreground">
            {resent ? `A new code is on its way to ${sentTo}.` : ""}
          </p>

          <p className="text-xs leading-relaxed text-muted-foreground">
            Codes expire after ten minutes and allow five attempts. Check your spam folder
            if it has not arrived.
          </p>
        </div>
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
            <div className="h-14 rounded-xl border border-hairline bg-surface/40" />
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
