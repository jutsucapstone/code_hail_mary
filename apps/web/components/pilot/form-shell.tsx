import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { Logo, Wordmark } from "@/components/site/logo";
import { siteConfig } from "@/lib/content";

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
}: {
  eyebrow: string;
  title: string;
  lead?: string;
  backHref: string;
  backLabel: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <div className="relative isolate flex flex-1 flex-col items-center px-6 py-10 lg:py-16">
      <div aria-hidden="true" className="absolute inset-0 -z-10 overflow-clip">
        <div className="hairline-grid radial-fade absolute inset-0" />
        <div className="glow-warm absolute left-1/2 top-[-18rem] h-[36rem] w-[52rem] max-w-[130vw] -translate-x-1/2 rounded-[50%] blur-[130px]" />
      </div>

      <div className="w-full max-w-lg">
        <Link
          href="/"
          className="flex w-fit items-center gap-2.5 rounded-md focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand"
        >
          <Logo className="h-7 w-7" />
          <Wordmark className="w-[4.75rem]" />
          <span className="sr-only">{siteConfig.name} home</span>
        </Link>

        <div className="mt-10 rounded-3xl border border-hairline bg-surface/40 p-7 sm:p-9">
          <p className="eyebrow flex items-center gap-2.5 text-brand">
            <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
            {eyebrow}
          </p>

          <h1 className="display mt-4 text-3xl font-semibold sm:text-4xl">{title}</h1>

          {lead ? (
            <p className="mt-4 text-pretty text-sm leading-relaxed text-muted-foreground">
              {lead}
            </p>
          ) : null}

          <div className="mt-8">{children}</div>
        </div>

        <div className="mt-6 flex items-center justify-between gap-4">
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
