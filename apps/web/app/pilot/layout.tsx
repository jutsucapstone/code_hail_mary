import type { Metadata } from "next";

import { MAIN_CONTENT_ID } from "@/lib/landmarks";

export const metadata: Metadata = {
  title: "Pilot access",
  description:
    "Set up JUTSU for your organisation, or join one that has already invited you.",
  // The funnel is public, but it is not content worth ranking, and the sub-routes carry
  // one-time tokens in their query strings. Keeping the whole subtree out of the index
  // means a verification link can never be surfaced by a crawler.
  robots: { index: false, follow: false },
};

/**
 * Shell for the pilot funnel.
 *
 * Deliberately not a route group: `(marketing)` and `(product)` exist because each holds
 * several sibling top-level segments that share chrome. `/pilot` is one segment, so it
 * takes a plain layout — a group whose entire contents live under one URL segment adds a
 * directory and buys nothing.
 *
 * No site header here. Someone part-way through creating an organisation should not be
 * offered the marketing nav; the only way out is the wordmark, back to the landing page.
 */
export default function PilotLayout({ children }: { children: React.ReactNode }) {
  return (
    <main id={MAIN_CONTENT_ID} className="relative isolate flex min-h-dvh flex-col">
      {children}
    </main>
  );
}
