import type { ReactNode } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { Container } from "@/components/site/section";
import { SiteFooter } from "@/components/site/site-footer";
import { SiteHeader } from "@/components/site/site-header";
import { MAIN_CONTENT_ID } from "@/lib/landmarks";

/**
 * Shell for the standalone legal pages.
 *
 * Uses the plain header rather than `SiteChrome` — the announcement bar is a
 * marketing surface and does not belong over a policy document — so the fixed
 * offset is a simple constant here instead of the measured `--chrome-h`.
 */
export function LegalPage({
  title,
  updated,
  intro,
  children,
}: {
  title: string;
  updated: string;
  intro: string;
  children: ReactNode;
}) {
  return (
    <>
      <div className="fixed inset-x-0 top-0 z-50">
        <SiteHeader />
      </div>

      <main id={MAIN_CONTENT_ID} className="flex-1 pb-24 pt-32 lg:pt-40">
        <Container>
          <Link
            href="/"
            className="group inline-flex items-center gap-2 rounded-md text-sm text-muted-foreground transition-colors hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            <ArrowLeft
              aria-hidden="true"
              className="size-4 transition-transform duration-300 group-hover:-translate-x-0.5"
            />
            Back to home
          </Link>

          <header className="mt-8 border-b border-hairline pb-8">
            <h1 className="display text-4xl font-semibold sm:text-5xl">{title}</h1>
            <p className="mt-4 font-mono text-xs uppercase tracking-[0.14em] text-muted-foreground">
              Last updated {updated}
            </p>
            <p className="mt-6 max-w-2xl text-pretty leading-relaxed text-muted-foreground">
              {intro}
            </p>
          </header>

          {/* Long-form copy: constrained measure, generous leading. */}
          <div
            className={[
              "mt-12 max-w-2xl",
              "[&_h2]:display [&_h2]:mt-12 [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:sm:text-2xl",
              "[&_p]:mt-4 [&_p]:text-pretty [&_p]:leading-relaxed [&_p]:text-muted-foreground",
              "[&_ul]:mt-4 [&_ul]:flex [&_ul]:flex-col [&_ul]:gap-2.5",
              "[&_li]:flex [&_li]:gap-3 [&_li]:text-pretty [&_li]:leading-relaxed [&_li]:text-muted-foreground",
              "[&_li]:before:mt-2.5 [&_li]:before:size-1 [&_li]:before:shrink-0 [&_li]:before:rounded-full [&_li]:before:bg-brand [&_li]:before:content-['']",
              "[&_a]:text-brand [&_a]:underline-offset-4 hover:[&_a]:underline",
            ].join(" ")}
          >
            {children}
          </div>
        </Container>
      </main>

      <SiteFooter />
    </>
  );
}
