"use client"; // Error boundaries must be Client Components.

import { RouteError } from "@/components/route-error";

/**
 * Wraps the KT console's pages. It does NOT wrap `kt/[code]/layout.tsx` in the same
 * segment, so a failure inside `KtShell` itself bubbles past this to `app/error.tsx`.
 *
 * Before this existed, a thrown error inside any KT tab unmounted the whole workspace —
 * shell, tabs and all — for a blank page. The other three route groups each had one of
 * these; the console that a brand-new employee is most likely to be sitting in did not.
 */
export default function Error({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return <RouteError error={error} retry={retry} what="this part of the workspace" />;
}
