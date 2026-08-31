"use client"; // Error boundaries must be Client Components.

import { RouteError } from "@/components/route-error";

/**
 * Wraps the six product surfaces.
 */
export default function Error({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return <RouteError error={error} retry={retry} what="this surface" />;
}
