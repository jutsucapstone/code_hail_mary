import type { Metadata } from "next";

import { MAIN_CONTENT_ID } from "@/lib/landmarks";

export const metadata: Metadata = {
  title: "Sign in",
  description: "Sign in to your JUTSU console with your JUTSU ID and work email.",
  // Not content worth ranking, and the flow continues into `/pilot/verify`, whose query
  // string carries a one-time token. Keeping the entry point out of the index too means
  // there is no crawlable path into that.
  robots: { index: false, follow: false },
};

/**
 * Shell for signing back in.
 *
 * Mirrors the pilot layout rather than sharing it: they are sibling top-level segments,
 * and hoisting a common layout to cover both would put every future top-level route
 * under it by default. The duplication is nine lines of chrome; the alternative silently
 * decides the shape of routes nobody has written yet.
 *
 * No site header, for the same reason the funnel has none — someone half-way through
 * authenticating should not be handed the marketing nav. The wordmark is the way out.
 */
export default function SignInLayout({ children }: { children: React.ReactNode }) {
  return (
    <main id={MAIN_CONTENT_ID} className="relative isolate flex min-h-dvh flex-col">
      {children}
    </main>
  );
}
