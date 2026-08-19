import type { Metadata } from "next";
import Link from "next/link";

import { Logo, Wordmark } from "@/components/site/logo";
import { ThemeToggle } from "@/components/site/theme-toggle";

import { SURFACES } from "@/lib/surfaces";
import { MAIN_CONTENT_ID } from "@/lib/landmarks";

export const metadata: Metadata = {
  // The product is behind auth; keeping it out of the index is deliberate.
  robots: { index: false, follow: false },
};

/**
 * Shell for the six product surfaces.
 *
 * Shares the marketing design tokens rather than introducing a second system — §16 is
 * explicit that a product looking unrelated to its own landing page reads as
 * unfinished.
 */
export default function ProductLayout({ children }: { children: React.ReactNode }) {
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
          </Link>

          <nav aria-label="Product" className="hidden lg:block">
            <ul className="flex items-center gap-1">
              {SURFACES.map((surface) => (
                <li key={surface.slug}>
                  <Link
                    href={`/${surface.slug}`}
                    className="rounded-md px-3 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                  >
                    {surface.name}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>

          <div className="flex items-center gap-3">
            {/* The organisation is no longer read from the cookie — there is nothing in
                it to read. Surfacing it here means asking GET /v1/me, which these six
                surfaces do not do yet, so the badge is simply absent rather than showing
                a placeholder that would be indistinguishable from real data (§4.11). */}
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main id={MAIN_CONTENT_ID} className="flex-1">{children}</main>
    </div>
  );
}
