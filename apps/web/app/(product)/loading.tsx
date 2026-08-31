import { RouteLoading } from "@/components/route-loading";

/**
 * Product surfaces open with a heading and a paragraph before any data.
 *
 * This wraps the segment's page in a Suspense boundary. It does not wrap the layout
 * above it, so the console chrome stays put and interactive while this renders.
 */
export default function Loading() {
  return <RouteLoading label="Loading this surface." rows={3} />;
}
