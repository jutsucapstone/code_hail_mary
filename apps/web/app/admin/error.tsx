"use client"; // Error boundaries must be Client Components.

import { RouteError } from "@/components/route-error";

/**
 * Wraps the admin pages. It does NOT wrap `admin/layout.tsx` in the same segment, so a
 * failure inside `AdminShell` itself bubbles past this to `app/error.tsx`.
 */
export default function Error({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return <RouteError error={error} retry={retry} what="this admin section" />;
}
