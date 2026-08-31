"use client"; // Error boundaries must be Client Components.

import { RouteError } from "@/components/route-error";

/**
 * Wraps the employee pages. A failure in `MemberShell` bubbles to `app/error.tsx`.
 */
export default function Error({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return <RouteError error={error} retry={retry} what="your console" />;
}
