"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, useEffect } from "react";
import { LogOut } from "lucide-react";

import { ConsoleNav, type ConsoleNavGroup, type ConsoleNavItem } from "@/components/console-nav";
import { FeedbackToggle } from "@/components/site/feedback-toggle";
import { Logo, Wordmark } from "@/components/site/logo";
import { ThemeToggle } from "@/components/site/theme-toggle";
import { FailureState, LoadingRegion, Skeleton } from "@/components/states";
import { api } from "@/lib/api";
import { classifyApiError, needsSignIn } from "@/lib/api-error";
import { MAIN_CONTENT_ID } from "@/lib/landmarks";
import { can, type Capabilities, type Permission } from "@/lib/permissions";
import { queryKeys } from "@/lib/query";
import { SIGN_IN_PATH } from "@/lib/surfaces";

/**
 * One shell for both consoles.
 *
 * `AdminShell` and `MemberShell` were two files that agreed on a header, a sign-out
 * handler, a `GET /v1/me` fetch, a capabilities context and a redirect, and disagreed —
 * for no reason a reader could infer — about what "loading" looks like. That is the same
 * failure `ConsoleNav` was extracted to fix one level down: two shells drifting teaches
 * people that the chrome means different things depending on which page they are on.
 *
 * The two consoles keep the differences that are real, as a `variant`:
 *
 * - **sidebar** (admin) — a permission-filtered section list beside a wide column, and an
 *   inner scroll area, because admin sections render tables.
 * - **inline** (member) — a horizontal section strip over a narrow column that scrolls as
 *   a page. A Member holds almost no permissions, and a sidebar filtered down to nothing
 *   reads as broken rather than as "this is your page".
 *
 * **The permission checks here hide doors. They do not lock them.** Every section's data
 * comes from an endpoint that re-checks server-side, so typing the URL of a hidden section
 * yields a 403 from the API rather than a rendered page. If this component were the
 * enforcement, anyone could edit it in devtools and let themselves in.
 */

const CapabilitiesContext = createContext<Capabilities | null>(null);

/**
 * The signed-in caller, as the API reported them.
 *
 * Non-null by construction: the shell renders a loading region until `GET /v1/me`
 * resolves, so a page never sees a half-loaded principal and never has to guard for one.
 */
export function useConsoleCapabilities(): Capabilities {
  const value = useContext(CapabilitiesContext);
  if (value === null) {
    throw new Error("useConsoleCapabilities must be used inside a ConsoleShell");
  }
  return value;
}

/** A navigable section, before the caller's permissions are known. */
export interface ShellSection {
  href: string;
  name: string;
  description: string;
  status: "live" | "pending";
  slice: string;
  /** `null` where the section needs nothing beyond an authenticated session. */
  permission: Permission | null;
  /** Optional IA grouping (§4). Ungrouped sections render as a flat list. */
  group?: string;
}

function visibleTo(
  capabilities: Capabilities,
  sections: readonly ShellSection[],
): ConsoleNavItem[] {
  return sections
    .filter((s) => s.permission === null || can(capabilities, s.permission))
    .map(({ href, name, description, status, slice, group }) => ({
      href,
      name,
      description,
      status,
      slice,
      group,
    }));
}

/** Preserve the authored order of groups rather than sorting them alphabetically. */
function group(items: ConsoleNavItem[]): ConsoleNavGroup[] {
  const order: string[] = [];
  const bucket = new Map<string, ConsoleNavItem[]>();

  for (const item of items) {
    const key = item.group ?? "";
    if (!bucket.has(key)) {
      bucket.set(key, []);
      order.push(key);
    }
    bucket.get(key)!.push(item);
  }

  return order.map((label) => ({ label: label || null, items: bucket.get(label)! }));
}

export function ConsoleShell({
  sections,
  navLabel,
  variant,
  children,
}: {
  sections: readonly ShellSection[];
  navLabel: string;
  variant: "sidebar" | "inline";
  children: React.ReactNode;
}) {
  const router = useRouter();

  const {
    data: capabilities,
    error,
    isPending,
    refetch,
  } = useQuery({
    queryKey: queryKeys.me,
    queryFn: api.me,
  });

  const failure = error ? classifyApiError(error) : null;
  const expired = failure !== null && needsSignIn(failure);

  useEffect(() => {
    // Only a 401 sends someone away. The previous shells redirected on *any* rejection,
    // so a transient 503 on `/v1/me` bounced a perfectly valid session to the sign-in
    // page — where signing in succeeds, lands back here, and fails again. A dependency
    // being down is not a reason to doubt who somebody is.
    if (expired) router.replace(SIGN_IN_PATH);
  }, [expired, router]);

  const sidebar = variant === "sidebar";

  return (
    // `h-dvh`, not `min-h-dvh`, and the overflow is clipped here — for the sidebar variant.
    //
    // This is what makes an inner scroll area possible at all. With a *minimum* height the
    // column simply grows past the viewport, so no flex child is ever forced to shrink and
    // `flex-1 overflow-auto` further down bounds nothing. Measured with 62 employee rows:
    // the page grew by 4068px and the table's scroller never scrolled a pixel.
    //
    // The inline variant deliberately keeps `min-h-dvh` and scrolls as a page: its column
    // is prose-width and holds no tables, and pinning the header there would cost a
    // reader on a short laptop screen most of the viewport.
    <div className={sidebar ? "flex h-dvh flex-col overflow-hidden" : "flex min-h-dvh flex-col"}>
      <header className="sticky top-0 z-50 border-b border-hairline bg-background/80 backdrop-blur-xl">
        <div
          className={
            sidebar
              ? "mx-auto flex h-16 w-full max-w-7xl items-center justify-between gap-6 px-6 [@media(max-height:820px)]:h-14 lg:px-8"
              : "mx-auto flex h-16 w-full max-w-3xl items-center justify-between gap-6 px-6 [@media(max-height:820px)]:h-14"
          }
        >
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
            <SignOutButton />
          </div>
        </div>
      </header>

      <div
        className={
          sidebar
            ? "mx-auto flex w-full min-h-0 max-w-7xl flex-1 flex-col gap-8 px-6 py-8 [@media(max-height:820px)]:gap-5 [@media(max-height:820px)]:py-4 lg:flex-row lg:gap-12 lg:px-8 lg:py-12 lg:[@media(max-height:820px)]:py-6"
            : "mx-auto flex w-full max-w-3xl flex-1 flex-col px-6 py-8 [@media(max-height:820px)]:py-6"
        }
      >
        {capabilities ? (
          sidebar ? (
            <ConsoleNav label={navLabel} groups={group(visibleTo(capabilities, sections))} />
          ) : (
            <div className="mb-8 border-b border-hairline pb-4">
              <ConsoleNav
                label={navLabel}
                orientation="horizontal"
                groups={[{ label: null, items: visibleTo(capabilities, sections) }]}
              />
            </div>
          )
        ) : null}

        {/* The landmark the skip link targets.

            It used to sit on a `<div>` in the route layout that wrapped the *entire*
            shell — header and navigation included — so "Skip to main content" skipped
            nothing at all, which is a WCAG 2.4.1 failure that looks exactly like a dead
            key press. The inline console had no `<main>` element whatsoever.

            The safety valve too, for the sidebar variant: a section whose content genuinely
            exceeds the viewport scrolls here rather than being clipped and unreachable —
            but because the box above has a definite height, a child asking for its own
            scroll area still gets one. */}
        <main
          id={MAIN_CONTENT_ID}
          className={
            sidebar
              ? "flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto"
              : "flex flex-1 flex-col"
          }
        >
          {capabilities ? (
            <CapabilitiesContext.Provider value={capabilities}>
              {children}
            </CapabilitiesContext.Provider>
          ) : expired ? (
            // Redirecting. Rendering an error here would flash a message nobody can act on
            // in the frame before the route changes.
            null
          ) : failure ? (
            // `FailureState` picks the screen: a denial reads as a denial, and the retry
            // button appears only where a retry could work. Without `onRetry` a reader
            // whose API blipped had no way forward but to reload the page by hand.
            <FailureState
              failure={failure}
              onRetry={() => void refetch()}
              deniedWhat="this console"
            />
          ) : isPending ? (
            <ShellSkeleton />
          ) : null}
        </main>
      </div>
    </div>
  );
}

function SignOutButton() {
  const router = useRouter();

  async function signOut() {
    // Server-side revocation, not just a cleared cookie: deleting the cookie alone leaves
    // the handle valid for anyone who captured it.
    await api.logout().catch(() => undefined);
    // Home, not the chooser and not sign-in. Someone who deliberately signed out is a
    // visitor again, and bouncing them onto a login form reads as refusing to let them
    // leave. The header carries Console, so coming back is one click.
    router.replace("/");
  }

  return (
    <button
      type="button"
      onClick={signOut}
      className="inline-flex items-center gap-2 rounded-lg border border-hairline-strong px-3 py-1.5 text-sm transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
    >
      <LogOut aria-hidden="true" className="size-4" />
      Sign out
    </button>
  );
}

/**
 * The one loading state both consoles use.
 *
 * The member shell used to refuse a skeleton here, on the reading that §4.11 forbids
 * standing in for data that has not arrived. The rule is about *mock data* — figures a
 * reader could mistake for real ones. These boxes are `aria-hidden` and carry no values
 * at all, so there is nothing to mistake; what a screen reader gets is the sentence in
 * `LoadingRegion`, which is more than the bare paragraph it replaces.
 */
function ShellSkeleton() {
  return (
    <LoadingRegion label="Loading your access.">
      <div aria-hidden="true" className="flex flex-col gap-6">
        <Skeleton className="h-10 w-64 rounded-xl" />
        <div className="grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-28 bg-background" />
          ))}
        </div>
      </div>
    </LoadingRegion>
  );
}
