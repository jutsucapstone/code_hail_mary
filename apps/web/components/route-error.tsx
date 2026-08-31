"use client";

import { useEffect } from "react";

import { ErrorState } from "@/components/states";

/**
 * What a route segment renders when something below it threw.
 *
 * One component behind every `error.tsx`, because six copies of an error screen is six
 * chances for the one a reader actually hits to be the one nobody styled.
 *
 * **The prop is `retry`, not `reset`.** Next 16.3 made `retry` stable and it is the one to
 * use: it re-fetches *and* re-renders the boundary's children, so a segment that failed on
 * a bad response can genuinely recover. `reset` only clears the error state and re-renders
 * with whatever is already in hand, which for a failed fetch means failing again
 * instantly — a "Try again" button that reliably does nothing.
 *
 * **`error.message` is not shown.** Errors thrown in a Server Component reach the client
 * with their message replaced by a generic string plus a digest, precisely so server
 * internals do not leak to a browser; errors from Client Components keep theirs, which
 * may be a stack-shaped string that means nothing to a reader and, in this application,
 * could carry a fragment of a document body. The digest is the safe half and the useful
 * half — it is what matches a line in the server log (§4.9).
 */
export function RouteError({
  error,
  retry,
  what,
}: {
  error: Error & { digest?: string };
  retry: () => void;
  /** What failed, in the reader's words. E.g. "this admin section". */
  what: string;
}) {
  useEffect(() => {
    // The browser console is the only sink here — there is no client error reporter, and
    // adding one is a stack decision rather than something to slip into a boundary. In
    // production this object carries a digest and a generic message, not the original.
    console.error(error);
  }, [error]);

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-16">
      <ErrorState
        message={`Something went wrong while loading ${what}. This is not something you did.`}
        requestId={error.digest}
        onRetry={retry}
      />
    </div>
  );
}
