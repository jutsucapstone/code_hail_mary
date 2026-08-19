"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useState } from "react";
import { LogOut } from "lucide-react";

import { Logo, Wordmark } from "@/components/site/logo";
import { ThemeToggle } from "@/components/site/theme-toggle";
import { ADMIN_SECTIONS, adminHref } from "@/lib/admin-nav";
import { api } from "@/lib/api";
import { can, type Capabilities } from "@/lib/permissions";
import { cn } from "@/lib/utils";

const CapabilitiesContext = createContext<Capabilities | null>(null);

/**
 * The signed-in caller, as the API reported them.
 *
 * Available only inside the shell, which guarantees it is non-null: the shell renders a
 * skeleton until the fetch resolves, so a page never sees a half-loaded principal and
 * never has to guard for one.
 */
export function useCapabilities(): Capabilities {
  const value = useContext(CapabilitiesContext);
  if (value === null) {
    throw new Error("useCapabilities must be used inside AdminShell");
  }
  return value;
}

/**
 * The admin chrome: identity, navigation, sign-out.
 *
 * It fetches `GET /v1/me` once and shares the result through context. The nav needs the
 * caller's permissions to decide which sections to show, and the page below needs them
 * too — fetching twice would mean two round trips and two chances to disagree about who
 * the caller is.
 *
 * **The permission checks here hide doors. They do not lock them.** Every section's data
 * comes from an endpoint that re-checks server-side, so typing the URL of a hidden
 * section yields a 403 from the API rather than a rendered page. If this component were
 * the enforcement, anyone could edit it in devtools and let themselves in.
 *
 * Visually it is the same design system as the marketing site — same tokens, same
 * hairlines, same micro-label — with a sidebar instead of a centred column, because §16
 * is explicit that a product looking unrelated to its own landing page reads as
 * unfinished.
 */
export function AdminShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [failed, setFailed] = useState(false);

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
        // expired, revoked, or the account was deactivated. Back to the front door.
        setFailed(true);
        router.replace("/pilot");
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function signOut() {
    // Server-side revocation, not just a cleared cookie: deleting the cookie alone leaves
    // the handle valid for anyone who captured it.
    await api.logout().catch(() => undefined);
    router.replace("/pilot");
  }

  const visible = ADMIN_SECTIONS.filter((section) =>
    can(capabilities, section.permission),
  );

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-50 border-b border-hairline bg-background/80 backdrop-blur-xl">
        <div className="mx-auto flex h-16 w-full max-w-7xl items-center justify-between gap-6 px-6 lg:px-8">
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

      <div className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-8 px-6 py-8 lg:flex-row lg:gap-12 lg:px-8 lg:py-12">
        <nav aria-label="Admin sections" className="lg:w-56 lg:shrink-0">
          <ul className="flex gap-1 overflow-x-auto lg:flex-col lg:overflow-visible">
            {visible.map((section) => {
              const href = adminHref(section.slug);
              const current = pathname === href;
              return (
                <li key={section.slug || "overview"} className="shrink-0">
                  <Link
                    href={href}
                    aria-current={current ? "page" : undefined}
                    className={cn(
                      "block rounded-lg px-3 py-2 text-sm transition-colors duration-200",
                      "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
                      current
                        ? "border border-brand/40 bg-brand/8 text-foreground"
                        : "border border-transparent text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {section.name}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        <main className="min-w-0 flex-1">
          {failed ? null : capabilities ? (
            <CapabilitiesContext.Provider value={capabilities}>
              {children}
            </CapabilitiesContext.Provider>
          ) : (
            <ShellSkeleton />
          )}
        </main>
      </div>
    </div>
  );
}

function ShellSkeleton() {
  return (
    <div role="status" aria-live="polite">
      <span className="sr-only">Loading your organisation.</span>
      <div aria-hidden="true" className="flex flex-col gap-6">
        <div className="h-10 w-64 animate-pulse rounded-xl bg-surface/60 motion-reduce:animate-none" />
        <div className="grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-28 bg-background" />
          ))}
        </div>
      </div>
    </div>
  );
}
