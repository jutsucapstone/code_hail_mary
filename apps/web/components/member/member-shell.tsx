"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useState } from "react";
import { LogOut } from "lucide-react";

import { FeedbackToggle } from "@/components/site/feedback-toggle";
import { Logo, Wordmark } from "@/components/site/logo";
import { ThemeToggle } from "@/components/site/theme-toggle";
import { api } from "@/lib/api";
import { SIGN_IN_PATH } from "@/lib/surfaces";
import type { Capabilities } from "@/lib/permissions";

const CapabilitiesContext = createContext<Capabilities | null>(null);

/** The signed-in member. Non-null by construction — the shell renders nothing until
 *  `GET /v1/me` answers, so a child never has to handle the loading case itself. */
export function useMemberCapabilities(): Capabilities {
  const value = useContext(CapabilitiesContext);
  if (!value) {
    throw new Error("useMemberCapabilities must be used inside MemberShell");
  }
  return value;
}

/**
 * Chrome for a person who belongs to an organisation without administering it.
 *
 * The same header as the admin shell — same tokens, same hairline, same wordmark —
 * minus the sidebar, because there are no sections a Member can open. §16 is explicit
 * that a product looking unrelated to its own landing page reads as unfinished, and
 * that applies just as much to the surface most people will actually see.
 *
 * `GET /v1/me` is the only call made here, and it is the only call a bare Member is
 * permitted: `profile:self_read` is in every role's set, while `org:read` is not. That
 * constraint is why this page shows a JUTSU ID and a role rather than an organisation
 * name — the name lives behind the organisation endpoint, and asking for it would earn
 * a 403 for exactly the people this page exists for.
 */
export function MemberShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) setCapabilities(me);
      })
      .catch(() => {
        if (cancelled) return;
        // The cookie was present — the layout checked — but the session is not usable:
        // expired, revoked, or the account was deactivated. Straight to sign-in: this
        // person has an account and needs a new session, not the chooser's question
        // about which sort of newcomer they are.
        router.replace(SIGN_IN_PATH);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function signOut() {
    // Server-side revocation, not just a cleared cookie: deleting the cookie alone
    // leaves the handle valid for anyone who captured it.
    await api.logout().catch(() => undefined);
    // Home, not the chooser and not sign-in. Someone who deliberately signed out is a
    // visitor again, and bouncing them onto a login form reads as refusing to let them
    // leave. The header carries Console, so coming back is one click.
    router.replace("/");
  }

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-50 border-b border-hairline bg-background/80 backdrop-blur-xl">
        <div className="mx-auto flex h-16 w-full max-w-3xl items-center justify-between gap-6 px-6 [@media(max-height:820px)]:h-14">
          <Link
            href="/"
            className="flex shrink-0 items-center gap-2.5 rounded-md focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand"
          >
            <Logo className="h-7 w-7" />
            <Wordmark className="w-[4.75rem]" />
            <span className="sr-only">JUTSU home</span>
          </Link>

          <div className="flex items-center gap-3">
            {/* The JUTSU ID, not a name or an email. It is the identifier a person reads
                out to support, and it carries no PII. */}
            {capabilities ? (
              <span className="hidden font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground sm:inline">
                {capabilities.jutsu_id ?? capabilities.role}
              </span>
            ) : null}
            <FeedbackToggle className="hidden sm:inline-flex" />
            <ThemeToggle />
            <button
              type="button"
              onClick={signOut}
              className="inline-flex items-center gap-2 rounded-lg border border-hairline-strong px-3 py-1.5 text-sm transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
            >
              <LogOut aria-hidden="true" className="size-4" />
              Sign out
            </button>
          </div>
        </div>
      </header>

      <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col px-6 py-12 [@media(max-height:820px)]:py-6">
        {capabilities ? (
          <CapabilitiesContext.Provider value={capabilities}>
            {children}
          </CapabilitiesContext.Provider>
        ) : (
          // No skeleton pretending to be content. One live region that says what is
          // happening, replaced by the real thing — §4.11 rules out standing in for data
          // that has not arrived.
          <p role="status" className="text-sm text-muted-foreground">
            Loading your access…
          </p>
        )}
      </div>
    </div>
  );
}
