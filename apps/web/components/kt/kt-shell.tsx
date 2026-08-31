"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { createContext, useContext } from "react";

import { Logo, Wordmark } from "@/components/site/logo";
import { ThemeToggle } from "@/components/site/theme-toggle";
import { LoadingRegion, Skeleton } from "@/components/states";
import { api, type KtRecipient } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import { MAIN_CONTENT_ID } from "@/lib/landmarks";
import { cn } from "@/lib/utils";

/**
 * The knowledge-transfer workspace shell.
 *
 * Deliberately NOT the employee console: this is a focused reading room over one
 * colleague's context, and it looks like one — the package's subject in the masthead,
 * the KT ID beside it, and a flat tab strip over the knowledge itself.
 *
 * **Authorization is re-established on every mount, and cached for seconds, not
 * sessions.** `POST /v1/kt/claim` runs each time the shell loads; a revoked or expired
 * package answers 403 with the exact sentence to display, and that is what renders —
 * whatever any earlier visit cached (§39). `staleTime: 0` on this one query is the
 * point, not an oversight.
 */

const KtContext = createContext<{ pkg: KtRecipient; code: string } | null>(null);

export function useKtPackage(): { pkg: KtRecipient; code: string } {
  const value = useContext(KtContext);
  if (value === null) {
    throw new Error("useKtPackage must be used inside KtShell");
  }
  return value;
}

/** The console's sections. Every entry renders; what each can show is decided by the
 *  package's scope and the platform's capabilities, honestly, on its own page. */
const TABS = [
  { slug: "", name: "Overview" },
  { slug: "documents", name: "Documents" },
  { slug: "ask", name: "Ask KT" },
  { slug: "projects", name: "Projects" },
  { slug: "responsibilities", name: "Responsibilities" },
  { slug: "people", name: "People" },
  { slug: "decisions", name: "Decisions" },
  { slug: "meetings", name: "Meetings" },
  { slug: "timeline", name: "Timeline" },
  { slug: "handover", name: "Handover" },
] as const;

export function KtShell({ code, children }: { code: string; children: React.ReactNode }) {
  const pathname = usePathname();

  const claim = useQuery({
    queryKey: ["kt", "open", code],
    queryFn: () => api.ktClaim(code),
    // Revocation must land on the next load, not after a cache window (§39).
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });

  const base = `/kt/${encodeURIComponent(code)}`;

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="border-b border-hairline bg-background">
        <div className="mx-auto flex w-full max-w-5xl flex-col gap-4 px-6 py-5">
          <div className="flex items-center justify-between gap-4">
            <Link
              href="/me"
              className="flex shrink-0 items-center gap-2.5 rounded-md focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand"
            >
              <Logo className="h-6 w-6" />
              <Wordmark className="w-16" />
              <span className="font-mono text-[0.625rem] uppercase tracking-[0.2em] text-muted-foreground">
                · Knowledge Transfer
              </span>
            </Link>
            <div className="flex items-center gap-3">
              <ThemeToggle />
              <Link
                href="/me"
                className="rounded-lg border border-hairline-strong px-3 py-1.5 text-sm transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
              >
                Exit workspace
              </Link>
            </div>
          </div>

          {claim.data ? (
            <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
              <h1 className="display text-2xl font-semibold">
                {claim.data.subject.display_name ?? "Knowledge package"}
              </h1>
              {claim.data.subject.designation ? (
                <span className="text-sm text-muted-foreground">
                  {claim.data.subject.designation}
                  {claim.data.subject.department ? ` · ${claim.data.subject.department}` : ""}
                </span>
              ) : null}
              <span className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground">
                {claim.data.kt_code}
              </span>
            </div>
          ) : null}

          {claim.data ? (
            <nav aria-label="Knowledge transfer sections" className="-mb-5 overflow-x-auto">
              <ul className="flex gap-1 pb-0">
                {TABS.map((tab) => {
                  const href = tab.slug ? `${base}/${tab.slug}` : base;
                  const current = pathname === href;
                  return (
                    <li key={tab.slug} className="shrink-0">
                      <Link
                        href={href}
                        aria-current={current ? "page" : undefined}
                        className={cn(
                          "block border-b-2 px-3 py-2.5 text-sm transition-colors",
                          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
                          current
                            ? "border-brand text-foreground"
                            : "border-transparent text-muted-foreground hover:text-foreground",
                        )}
                      >
                        {tab.name}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </nav>
          ) : null}
        </div>
      </header>

      <main id={MAIN_CONTENT_ID} className="mx-auto w-full max-w-5xl flex-1 px-6 py-8">
        {claim.data ? (
          <KtContext.Provider value={{ pkg: claim.data, code: claim.data.kt_code }}>
            {children}
          </KtContext.Provider>
        ) : claim.error ? (
          <KtRefusal error={claim.error} />
        ) : (
          <LoadingRegion label="Opening the knowledge-transfer package.">
            <div className="flex flex-col gap-4">
              <Skeleton className="h-8 w-64" />
              <Skeleton className="h-40" />
            </div>
          </LoadingRegion>
        )}
      </main>
    </div>
  );
}

/**
 * A refused open, in the server's own words.
 *
 * Not the shared FailureState: that renders a 403 as "your role does not include…",
 * which is the wrong sentence here — a revoked package is not a permission problem,
 * and §39 requires the exact revocation copy the API sends. The message IS the API's.
 */
function KtRefusal({ error }: { error: unknown }) {
  const failure = classifyApiError(error);
  const title =
    failure.kind === "denied"
      ? "This package is closed"
      : failure.kind === "missing"
        ? "No package matches that ID"
        : "That did not load";
  return (
    <div
      role="alert"
      className="flex flex-col items-start gap-3 rounded-2xl border border-hairline bg-surface/40 p-8"
    >
      <h2 className="display text-xl font-semibold">{title}</h2>
      <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
        {failure.message}
      </p>
      <Link
        href="/handover"
        className="mt-2 rounded-lg border border-hairline-strong px-3.5 py-2 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
      >
        Back to Knowledge Transfer
      </Link>
    </div>
  );
}
