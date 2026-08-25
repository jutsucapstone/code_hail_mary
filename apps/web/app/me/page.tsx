"use client";

import { IdCard, ShieldCheck, Sparkles } from "lucide-react";

import { useMemberCapabilities } from "@/components/member/member-shell";
import { ROLE_LABELS } from "@/lib/permissions";

/**
 * Where onboarding ends for everyone who is not an administrator.
 *
 * Its job is confirmation, not features. Someone has just typed a six-digit code and
 * been redirected; the first question they have is "did that work, and what am I now".
 * So it answers with the two facts that are true and theirs — the JUTSU ID that was
 * issued to them, and the role it was issued under — and then says plainly what does
 * and does not exist yet.
 *
 * Everything rendered comes from `GET /v1/me`, which is the only endpoint a bare Member
 * may call. No organisation name, because that needs `org:read` and a Member does not
 * hold it; inventing one, or showing a raw tenant UUID in its place, would both be
 * worse than not showing it (§4.11).
 */
export default function MePage() {
  const capabilities = useMemberCapabilities();
  const roleLabel = ROLE_LABELS[capabilities.role] ?? capabilities.role;

  return (
    <div className="flex flex-col gap-10 [@media(max-height:820px)]:gap-6">
      <header>
        <p className="eyebrow flex items-center gap-2.5 text-brand">
          <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
          Your access
        </p>
        <h1 className="display mt-4 text-3xl font-semibold [@media(max-height:820px)]:mt-2 [@media(max-height:820px)]:text-2xl sm:text-4xl">
          You&rsquo;re set up
        </h1>
        <p className="mt-3 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Your account is active and your JUTSU ID has been issued. Keep it — it is how
          you are identified across the product, and it is what you quote if you ever
          need support.
        </p>
      </header>

      <section aria-labelledby="identity-heading">
        <h2 id="identity-heading" className="sr-only">
          Your identity
        </h2>
        <dl className="grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-2">
          <div className="flex flex-col gap-3 bg-background p-6 [@media(max-height:820px)]:gap-2 [@media(max-height:820px)]:p-4">
            <span
              aria-hidden="true"
              className="flex size-9 items-center justify-center rounded-lg border border-hairline-strong bg-surface text-brand"
            >
              <IdCard className="size-4" />
            </span>
            <dd className="font-mono text-lg text-foreground">
              {capabilities.jutsu_id ?? "Not issued"}
            </dd>
            <dt className="text-sm text-muted-foreground">Your JUTSU ID</dt>
          </div>
          <div className="flex flex-col gap-3 bg-background p-6 [@media(max-height:820px)]:gap-2 [@media(max-height:820px)]:p-4">
            <span
              aria-hidden="true"
              className="flex size-9 items-center justify-center rounded-lg border border-hairline-strong bg-surface text-brand"
            >
              <ShieldCheck className="size-4" />
            </span>
            <dd className="text-lg text-foreground">{roleLabel}</dd>
            <dt className="text-sm text-muted-foreground">Your role</dt>
          </div>
        </dl>
      </section>

      <section
        aria-labelledby="next-heading"
        className="flex items-start gap-4 rounded-2xl border border-hairline bg-surface/40 p-6 [@media(max-height:820px)]:p-4"
      >
        <span
          aria-hidden="true"
          className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-hairline-strong bg-surface text-brand"
        >
          <Sparkles className="size-4" />
        </span>
        <div>
          <h2 id="next-heading" className="display text-lg font-semibold">
            What happens next
          </h2>
          <p className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
            Connecting your own accounts is the next step, and it is not built yet.
            Nothing is connected on your behalf and nothing is read from your tools until
            you connect them yourself. Your administrator will let you know when that
            opens.
          </p>
        </div>
      </section>
    </div>
  );
}
