import { RouteLoading } from "@/components/route-loading";

/**
 * The employee console is a prose-width column — no stat strip to stand in for.
 *
 * This wraps the segment's page in a Suspense boundary. It does not wrap the layout
 * above it, so the console chrome stays put and interactive while this renders.
 */
export default function Loading() {
  return <RouteLoading label="Loading your console." rows={3} />;
}
