"use client";

import Link from "next/link";
import { IdCard, Plug, Search, ShieldCheck, Sparkles } from "lucide-react";

import { useMemberCapabilities } from "@/components/member/member-shell";
import { Button } from "@/components/ui/button";
import { can, ROLE_LABELS } from "@/lib/permissions";

/**
 * Where onboarding ends for everyone who is not an administrator.
 *
 * It confirms first: someone has just typed a six-digit code and been redirected, and
 * the first question they have is "did that work, and what am I now". So it answers
 * with the two facts that are true and theirs — the JUTSU ID that was issued to them,
 * and the role it was issued under.
 *
 * Then it hands over the next step rather than describing one. This page used to name
 * Integrations in prose and offer no way to get there, which leaves the reader to find
 * the sidebar entry the sentence is talking about; the two things a new employee can
 * actually do on day one are now the two controls under that sentence, and both point
 * at routes that exist and are live in `MEMBER_SECTIONS`.
 *
 * Asking is offered only to a caller who holds `retrieval:query`. Rendering it
 * regardless would be a door onto a 403 — and `can()` here decides what to draw, never
 * what is allowed: `/v1/ask` re-checks server-side whatever this believed.
 *
 * Everything rendered comes from `GET /v1/me`, which is the only endpoint a bare Member
 * may call. No organisation name, because that needs `org:read` and a Member does not
 * hold it; inventing one, or showing a raw tenant UUID in its place, would both be
 * worse than not showing it (§4.11).
 */
export default function MePage() {
  const capabilities = useMemberCapabilities();
  const roleLabel = ROLE_LABELS[capabilities.role] ?? capabilities.role;
  const canAsk = can(capabilities, "retrieval:query");

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
            Connect your own work tools — nothing is connected on your behalf, and
            nothing is read from a tool until you connect it yourself. What you can
            connect is governed by your organisation&apos;s policies, and you can
            disconnect at any time.
          </p>

          <div className="mt-5 flex flex-wrap gap-3">
            <Button
              asChild
              size="lg"
              className="h-10 rounded-xl bg-brand px-4 font-semibold text-brand-foreground hover:bg-brand/90 focus-visible:ring-brand/40"
            >
              <Link href="/me/integrations">
                <Plug aria-hidden="true" />
                Connect a tool
              </Link>
            </Button>

            {canAsk ? (
              <Button
                asChild
                size="lg"
                variant="outline"
                className="h-10 rounded-xl border-hairline-strong bg-transparent px-4 hover:border-brand/40 hover:bg-brand/5 dark:bg-transparent dark:hover:bg-brand/5"
              >
                <Link href="/ask">
                  <Search aria-hidden="true" />
                  Ask a question
                </Link>
              </Button>
            ) : null}
          </div>

          {canAsk ? (
            <p className="mt-3 max-w-prose text-pretty text-xs leading-relaxed text-muted-foreground">
              You can ask before you connect anything. Ask JUTSU searches only the
              documents you are already authorised to read, so a new account with nothing
              connected will simply find less.
            </p>
          ) : null}
        </div>
      </section>
    </div>
  );
}
