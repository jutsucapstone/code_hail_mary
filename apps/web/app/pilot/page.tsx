import Link from "next/link";
import { ArrowRight, Building2, IdCard, Lock, type LucideIcon } from "lucide-react";

import { IllustrationReel } from "@/components/pilot/illustration-reel";
import { Logo, Wordmark } from "@/components/site/logo";
import { pilot, siteConfig } from "@/lib/content";

/**
 * Icons live component-side, keyed by the `icon` field on each path.
 *
 * Same idiom the marketing sections use: `lib/content.ts` stays JSON-serialisable and
 * free of React imports, so the copy remains portable to a CMS.
 */
const PATH_ICONS: Record<string, LucideIcon> = {
  org: Building2,
  employee: IdCard,
};

/**
 * The illustration reel shown in the brand panel.
 *
 * Listed here rather than globbed from the directory so the order is deliberate and a
 * missing file is a visible 404 during review rather than a silently shorter loop.
 */
const PILOT_ILLUSTRATIONS = [
  { src: "/illustrations/pilot-01.png", width: 227, height: 207 },
  { src: "/illustrations/pilot-02.png", width: 222, height: 205 },
  { src: "/illustrations/pilot-03.png", width: 197, height: 212 },
  { src: "/illustrations/pilot-04.png", width: 248, height: 302 },
  { src: "/illustrations/pilot-05.png", width: 302, height: 307 },
] as const;

export default function PilotPage() {
  return (
    <div className="mx-auto grid w-full max-w-7xl flex-1 gap-10 px-6 py-10 [@media(max-height:820px)]:gap-6 [@media(max-height:820px)]:py-5 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] lg:items-stretch lg:gap-16 lg:px-8 lg:py-16 lg:[@media(max-height:820px)]:py-6">
      {/* ---------------------------------------------------------------- brand panel */}
      <aside className="relative isolate hidden overflow-hidden rounded-3xl border border-hairline bg-surface/40 p-10 [@media(max-height:820px)]:p-6 lg:flex lg:flex-col">
        <div aria-hidden="true" className="absolute inset-0 -z-10 overflow-clip">
          <div className="hairline-grid radial-fade absolute inset-0" />
          <div className="glow-warm absolute -left-24 top-[-12rem] h-[34rem] w-[34rem] rounded-full blur-[120px]" />
        </div>

        <Link
          href="/"
          className="flex w-fit shrink-0 items-center gap-2.5 rounded-md focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand"
        >
          <Logo className="h-8 w-8" />
          <Wordmark className="w-[5.25rem]" />
          <span className="sr-only">{siteConfig.name} home</span>
        </Link>

        {/* The reel sits between the lockup and the tagline, taking the space the
            description used to. It is decorative and carries no copy, so nothing that
            was being read is lost — the two choice cards opposite are what this page is
            for, and a paragraph of product prose here competed with them. */}
        <div className="flex min-h-0 flex-1 items-center justify-center py-10 [@media(max-height:820px)]:py-4">
          {/* 15rem, not larger. The source artwork is 197-307px on its longest side, so
              a wider stage upscales it and the line work goes soft — the frame is sized
              to the art rather than the art stretched to the frame. */}
          <IllustrationReel
            illustrations={PILOT_ILLUSTRATIONS}
            className="max-w-[15rem]"
            sizes="(min-width: 1024px) 15rem, 0px"
          />
        </div>

        <div>
          <p className="display text-pretty text-3xl font-semibold leading-[1.1]">
            {siteConfig.tagline}
          </p>

          <ul className="mt-8 flex flex-col gap-3 border-t border-hairline pt-8">
            {pilot.reassurance.map((line) => (
              <li key={line} className="flex items-start gap-3">
                <Lock
                  aria-hidden="true"
                  className="mt-0.5 size-4 shrink-0 text-brand"
                />
                <span className="text-sm leading-relaxed text-muted-foreground">
                  {line}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </aside>

      {/* ------------------------------------------------------------------- chooser */}
      <section aria-labelledby="pilot-heading" className="flex flex-col justify-center">
        {/* The lockup repeats on narrow viewports, where the brand panel is hidden and
            this would otherwise be an unbranded form. */}
        <Link
          href="/"
          className="mb-10 flex w-fit items-center gap-2.5 rounded-md focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand lg:hidden"
        >
          <Logo className="h-7 w-7" />
          <Wordmark className="w-[4.75rem]" />
          <span className="sr-only">{siteConfig.name} home</span>
        </Link>

        <p className="eyebrow flex items-center gap-2.5 text-brand">
          <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
          {pilot.eyebrow}
        </p>

        <h1
          id="pilot-heading"
          className="display mt-5 text-4xl font-semibold [@media(max-height:820px)]:mt-3 [@media(max-height:820px)]:text-3xl sm:text-5xl"
        >
          {pilot.title}
        </h1>

        <p className="mt-5 max-w-xl text-pretty text-base leading-relaxed text-muted-foreground [@media(max-height:820px)]:mt-3 sm:text-lg">
          {pilot.lead}
        </p>

        <div className="spotlight-group mt-10 flex flex-col gap-4 [@media(max-height:820px)]:mt-6 [@media(max-height:820px)]:gap-3">
          {pilot.paths.map((path) => {
            const Icon = PATH_ICONS[path.icon];
            return (
              <Link
                key={path.id}
                href={path.href}
                className="spotlight group relative flex items-start gap-5 rounded-2xl border border-hairline bg-surface/40 p-6 transition-colors duration-300 hover:border-brand/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand [@media(max-height:820px)]:p-4 sm:p-7"
              >
                <span
                  aria-hidden="true"
                  className="flex size-11 shrink-0 items-center justify-center rounded-xl border border-hairline-strong bg-surface text-brand transition-colors duration-300 group-hover:border-brand/40 group-hover:bg-brand/8"
                >
                  {Icon ? <Icon className="size-5" /> : null}
                </span>

                <span className="flex min-w-0 flex-col">
                  {/* Full-strength muted, not the /80 tier used elsewhere: these sit on a
                      bg-surface/40 card, which in the light theme is lighter than the page
                      ground the /80 floor was tuned against. Measured 4.32:1 there — below AA. */}
                  <span className="eyebrow text-muted-foreground">{path.role}</span>
                  <span className="display mt-2 text-xl font-semibold text-foreground sm:text-[1.375rem]">
                    {path.label}
                  </span>
                  <span className="mt-2.5 text-pretty text-sm leading-relaxed text-muted-foreground">
                    {path.description}
                  </span>
                  <span className="mt-4 font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground">
                    {path.note}
                  </span>
                </span>

                <ArrowRight
                  aria-hidden="true"
                  className="ml-auto mt-1 size-5 shrink-0 text-muted-foreground transition-all duration-300 group-hover:translate-x-0.5 group-hover:text-brand"
                />
              </Link>
            );
          })}
        </div>

        <p className="mt-8 max-w-xl text-xs leading-relaxed text-muted-foreground/80 [@media(max-height:820px)]:mt-4">
          By continuing you accept the{" "}
          <Link
            href="/terms"
            className="rounded text-foreground underline underline-offset-4 transition-colors hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            Terms of Service
          </Link>{" "}
          and acknowledge our{" "}
          <Link
            href="/privacy"
            className="rounded text-foreground underline underline-offset-4 transition-colors hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            Privacy Policy
          </Link>
          .
        </p>
      </section>
    </div>
  );
}
