import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { SurfaceStub } from "@/components/site/surface-stub";
import { surfaceBySlug } from "@/lib/surfaces";

const SLUG = "handover";

export function generateMetadata(): Metadata {
  const surface = surfaceBySlug(SLUG);
  return { title: surface?.name ?? SLUG, description: surface?.purpose };
}

export default function Page() {
  const surface = surfaceBySlug(SLUG);
  // The route tree and lib/surfaces.ts are meant to stay in lockstep; if they drift,
  // fail visibly rather than rendering a nameless page.
  if (!surface) notFound();
  return <SurfaceStub surface={surface} />;
}
