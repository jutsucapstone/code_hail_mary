import { RouteLoading } from "@/components/route-loading";

/**
 * Admin sections lead with a stat strip and then a table, so the fallback does too.
 *
 * This wraps the segment's page in a Suspense boundary. It does not wrap the layout
 * above it, so the console chrome stays put and interactive while this renders.
 */
export default function Loading() {
  return <RouteLoading label="Loading this admin section." rows={5} wide />;
}
