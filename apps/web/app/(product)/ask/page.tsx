import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { Container } from "@/components/site/section";
import { EvidenceSearch, RetrievalOnlyNotice } from "@/components/product/evidence-search";
import { surfaceBySlug } from "@/lib/surfaces";

const SLUG = "ask";

/**
 * Cited Q&A — the retrieval half of it.
 *
 * `POST /v1/search` is real and ACL-filtered, so this page runs it. Answer synthesis is
 * slice S18–S19 and does not exist, so this page does not pretend to it: the surface
 * stays `stub` in `lib/surfaces.ts` and `RetrievalOnlyNotice` says which half shipped.
 *
 * That is §4.11 read precisely. The rule forbids mock data behind a surface — every
 * passage here is a real document the caller is permitted to read. It does not require a
 * surface to stay dark until it is complete, only that it never overstate what it is.
 */
export function generateMetadata(): Metadata {
  const surface = surfaceBySlug(SLUG);
  return { title: surface?.name ?? SLUG, description: surface?.purpose };
}

export default function Page() {
  const surface = surfaceBySlug(SLUG);
  // The route tree and lib/surfaces.ts are meant to stay in lockstep; if they drift,
  // fail visibly rather than rendering a nameless page.
  if (!surface) notFound();

  return (
    <Container className="py-16 lg:py-24">
      <div className="max-w-3xl">
        <p className="eyebrow flex items-center gap-2.5 text-brand">
          <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
          {surface.kind === "differentiator" ? "Differentiator" : "Table stakes"}
        </p>

        <h1 className="display mt-5 text-4xl font-semibold sm:text-5xl">{surface.name}</h1>

        <p className="mt-6 text-pretty text-base leading-relaxed text-muted-foreground sm:text-lg">
          {surface.purpose}
        </p>

        <RetrievalOnlyNotice slice={surface.slice} />

        <EvidenceSearch />
      </div>
    </Container>
  );
}
