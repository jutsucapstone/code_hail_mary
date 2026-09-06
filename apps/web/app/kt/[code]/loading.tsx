import { RouteLoading } from "@/components/route-loading";

/**
 * The KT tabs lead with a heading and a list of cards, so the fallback does too.
 *
 * Wraps the segment's page in a Suspense boundary, not the layout above it — the shell,
 * its masthead and its tab strip stay put and interactive while a tab streams in.
 */
export default function Loading() {
  return <RouteLoading label="Loading this part of the workspace." rows={4} />;
}
