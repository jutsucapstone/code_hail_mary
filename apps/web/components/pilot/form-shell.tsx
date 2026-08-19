import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { Logo, Wordmark } from "@/components/site/logo";
import { siteConfig } from "@/lib/content";
import { cn } from "@/lib/utils";

/**
 * Chrome shared by every step of the pilot funnel.
 *
 * A single centred column rather than the two-panel chooser: past the fork, the job is to
 * fill in one form, and a decorative half-screen competes with that.
 *
 * The back link is not a browser-history call. Someone who arrives at a verification step
 * from an email has no history to go back to, and a control that sometimes does nothing
 * is worse than one that always goes somewhere predictable.
 */
export function FormShell({
  eyebrow,
  title,
  lead,
  backHref,
  backLabel,
  children,
  footer,
  size = "default",
}: {
  eyebrow: string;
  title: string;
  lead?: string;
  backHref: string;
  backLabel: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  /**
   * "wide" lays the card out landscape so a multi-field form fits without scrolling.
   *
   * Six fields stacked in a 32rem column run to roughly 950px of card — taller than a
   * 900px laptop viewport, so the submit button sits below the fold and the person
   * filling the form in cannot see what they are committing to. Widening the card and
   * pairing the fields turns six rows into three.
   */
  size?: "default" | "wide";
}) {
  return (
    // Vertical rhythm is deliberately tighter than the marketing pages, and the column is
    // centred rather than top-aligned. This is a form to be completed, not a page to be
    // read, and every rem of padding pushes the submit button closer to the fold.
    //
    // The `max-height` variants compact the whole shell on short viewports — a 720p
    // laptop, or a 900p one at 125% scaling, both of which are ordinary. Tightening
    // globally instead would make the comfortable case feel cramped to buy nothing, so
    // the spacing gives way only when the screen actually demands it.
    <div className="relative isolate flex flex-1 flex-col items-center justify-center px-6 py-8 [@media(max-height:820px)]:py-4 lg:py-10">
      <div aria-hidden="true" className="absolute inset-0 -z-10 overflow-clip">
        <div className="hairline-grid radial-fade absolute inset-0" />
        <div className="glow-warm absolute left-1/2 top-[-18rem] h-[36rem] w-[52rem] max-w-[130vw] -translate-x-1/2 rounded-[50%] blur-[130px]" />
      </div>

      <div className={cn("w-full", size === "wide" ? "max-w-3xl" : "max-w-lg")}>
        <Link
          href="/"
          className="flex w-fit items-center gap-2.5 rounded-md focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand"
        >
          <Logo className="h-7 w-7" />
          <Wordmark className="w-[4.75rem]" />
          <span className="sr-only">{siteConfig.name} home</span>
        </Link>

        <div className="mt-8 rounded-3xl border border-hairline bg-surface/40 p-7 [@media(max-height:820px)]:mt-4 [@media(max-height:820px)]:p-5 sm:p-8">
          <p className="eyebrow flex items-center gap-2.5 text-brand">
            <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
            {eyebrow}
          </p>

          <h1 className="display mt-4 text-2xl font-semibold [@media(max-height:820px)]:mt-2 [@media(max-height:820px)]:text-xl sm:text-3xl">
            {title}
          </h1>

          {lead ? (
            <p className="mt-4 text-pretty text-sm leading-relaxed text-muted-foreground [@media(max-height:820px)]:mt-2">
              {lead}
            </p>
          ) : null}

          <div className="mt-6 [@media(max-height:820px)]:mt-4">{children}</div>
        </div>

        <div className="mt-6 flex items-center justify-between gap-4 [@media(max-height:820px)]:mt-4">
          <Link
            href={backHref}
            className="inline-flex items-center gap-2 rounded text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            <ArrowLeft aria-hidden="true" className="size-4" />
            {backLabel}
          </Link>
          {footer}
        </div>
      </div>
    </div>
  );
}
