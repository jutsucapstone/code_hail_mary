"use client"; // Error boundaries must be Client Components.

import { RouteError } from "@/components/route-error";

/**
 * Anything below the root layout that has no closer boundary of its own.
 */
export default function Error({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return <RouteError error={error} retry={retry} what="this page" />;
}
