import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { Container } from "@/components/site/section";
import { AskExperience } from "@/components/product/ask-experience";
import { surfaceBySlug } from "@/lib/surfaces";

const SLUG = "ask";

/**
 * Cited Q&A — both halves now.
 *
 * `POST /v1/ask` retrieves under the caller's ACL and composes a grounded answer whose
 * every citation was validated server-side against the retrieved set; uncited or
 * hallucinated answers are refused as insufficient_evidence rather than rendered. On a
 * deployment with no answer provider the component degrades to retrieval with the
 * reason on screen — never a dead Ask box, never a fake answer (§4.11).
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

        <AskExperience />
      </div>
    </Container>
  );
}
