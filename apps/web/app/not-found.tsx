import type { Metadata } from "next";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { Logo, Wordmark } from "@/components/site/logo";
import { Container } from "@/components/site/section";
import { nav } from "@/lib/content";
import { MAIN_CONTENT_ID } from "@/lib/landmarks";

export const metadata: Metadata = {
  title: "Page not found",
  robots: { index: false, follow: true },
};

/**
 * Branded 404. Standalone rather than wrapped in the site chrome — there is no
 * page to be "in", so the mark and a way back are the whole job.
 */
export default function NotFound() {
  return (
    <main
      id={MAIN_CONTENT_ID}
      className="relative isolate flex min-h-dvh flex-col items-center justify-center py-24"
    >
      <div aria-hidden="true" className="absolute inset-0 -z-10 overflow-clip">
        <div className="hairline-grid radial-fade absolute inset-0" />
        <div className="glow-warm absolute left-1/2 top-1/4 h-[28rem] w-[46rem] max-w-[130vw] -translate-x-1/2 rounded-[50%] blur-[130px]" />
      </div>

      <Container className="flex flex-col items-center text-center">
        <Link
          href="/"
          className="flex items-center gap-2.5 rounded-md focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand"
        >
          <Logo className="h-8 w-8" />
          <Wordmark className="w-24" />
        </Link>

        <p className="eyebrow mt-12 text-brand">Error 404</p>
        <h1 className="display mt-5 text-4xl font-semibold sm:text-5xl">
          This page is not in memory.
        </h1>
        <p className="mt-5 max-w-md text-pretty leading-relaxed text-muted-foreground">
          The address you followed does not match anything here. It may have moved, or it
          may never have existed.
        </p>

        <Link
          href="/"
          className="group mt-9 inline-flex h-12 items-center gap-2 rounded-xl bg-brand px-6 text-[0.9375rem] font-semibold text-brand-foreground transition-colors hover:bg-brand/90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
        >
          <ArrowLeft
            aria-hidden="true"
            className="size-4 transition-transform duration-300 group-hover:-translate-x-0.5"
          />
          Back to home
        </Link>

        <nav aria-label="Site sections" className="mt-14 border-t border-hairline pt-8">
          <ul className="flex flex-wrap items-center justify-center gap-x-6 gap-y-3">
            {nav.map((item) => (
              <li key={item.href}>
                <Link
                  href={`/${item.href}`}
                  className="rounded text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      </Container>
    </main>
  );
}
