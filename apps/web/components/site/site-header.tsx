"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { motion, useReducedMotion } from "framer-motion";
import { LogIn, Menu, X } from "lucide-react";

import { Logo, Wordmark } from "@/components/site/logo";
import { ScrollProgress } from "@/components/site/scroll-progress";
import { FeedbackToggle } from "@/components/site/feedback-toggle";
import { ThemeToggle } from "@/components/site/theme-toggle";
import { Button } from "@/components/ui/button";
import { consoleCta, contact, nav, siteConfig } from "@/lib/content";
import { SIGN_IN_PATH } from "@/lib/surfaces";
import { cn } from "@/lib/utils";

/**
 * Which section owns the viewport right now, for the nav's active underline.
 *
 * One observer over the sections the nav actually links to, watching a band just
 * above the vertical middle: a heading crossing eye level is what a reader means
 * by "I am in this section now". Ties resolve to the entry nearest the top, and
 * scrolling back above the first section clears the underline entirely — the nav
 * should not claim you are reading "Problem" while the hero fills the screen.
 */
function useActiveSection(ids: readonly string[]) {
  const [active, setActive] = useState<string | null>(null);

  useEffect(() => {
    const sections = ids
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => el !== null);
    if (sections.length === 0) return;

    const visible = new Map<string, number>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) visible.set(entry.target.id, entry.boundingClientRect.top);
          else visible.delete(entry.target.id);
        }
        if (visible.size === 0) {
          setActive(null);
          return;
        }
        const [topmost] = [...visible.entries()].sort((a, b) => a[1] - b[1]);
        setActive(topmost[0]);
      },
      // The band: from just under the chrome down to 45% of the viewport.
      { rootMargin: "-15% 0px -55% 0px" },
    );
    for (const section of sections) observer.observe(section);
    return () => observer.disconnect();
  }, [ids]);

  return active;
}

// Nav hrefs are `/#hash` so they survive the legal pages; the section ids carry
// no slash, so the whole prefix comes off, not just the `#`.
const NAV_SECTION_IDS = nav.map((item) => item.href.replace("/#", ""));

export function SiteHeader() {
  const [scrolled, setScrolled] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const activeSection = useActiveSection(NAV_SECTION_IDS);
  const shouldReduceMotion = useReducedMotion();

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 24);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // Lock the page while the mobile sheet is open, and let Escape close it.
  useEffect(() => {
    if (!menuOpen) return;

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);

    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  return (
    <header
      className={cn(
        "relative transition-[background-color,border-color,backdrop-filter,box-shadow] duration-300",
        scrolled || menuOpen
          ? "border-b border-hairline bg-background/85 shadow-lg shadow-foreground/[0.04] backdrop-blur-xl"
          : "border-b border-transparent bg-transparent",
      )}
    >
      <div
        className={cn(
          "mx-auto flex w-full max-w-7xl items-center justify-between gap-6 px-6 transition-[height] duration-300 lg:px-8",
          scrolled ? "h-14 lg:h-15" : "h-16 lg:h-18",
        )}
      >
        <Link
          href="/#hero"
          className="group flex items-center gap-2.5 rounded-md focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand"
          aria-label={`${siteConfig.name} — home`}
        >
          <Logo
            priority
            className="h-7 w-7 shrink-0 transition-transform duration-300 group-hover:scale-105"
          />
          <Wordmark className="w-[4.75rem]" />
        </Link>

        <nav aria-label="Primary" className="hidden lg:block">
          <ul className="flex items-center gap-1">
            {nav.map((item) => {
              const isActive = activeSection === item.href.replace("/#", "");
              return (
                <li key={item.href} className="relative">
                  <a
                    href={item.href}
                    aria-current={isActive ? "true" : undefined}
                    className={cn(
                      "rounded-md px-3 py-2 text-sm transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
                      isActive
                        ? "text-foreground"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {item.label}
                  </a>
                  {/* One underline slides between links (a shared layoutId) rather
                      than five fading in and out — the sliding is what tells the
                      eye these are positions on one track, not separate toggles. */}
                  {isActive ? (
                    <motion.span
                      layoutId="site-nav-underline"
                      transition={
                        shouldReduceMotion
                          ? { duration: 0 }
                          : { type: "spring", stiffness: 500, damping: 40 }
                      }
                      className="absolute inset-x-3 bottom-1 h-[2px] rounded-full bg-brand"
                      aria-hidden="true"
                    />
                  ) : null}
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="flex items-center gap-2">
          <FeedbackToggle className="hidden sm:inline-flex" />
          <ThemeToggle className="hidden sm:inline-flex" />
          {/* A hairline seam between the page's controls and the account doors:
              two different kinds of act, and the seam says so without a label. */}
          <span aria-hidden="true" className="mx-1 hidden h-5 w-px bg-hairline-strong sm:block" />
          {/* The way back in, for people who already have an account.
              Outlined rather than filled: the marketing page's job is still to convert a
              visitor, so this must not compete with "Request a pilot" — but a returning
              customer looks top-right for it, and having no door at all sent them into
              the pilot funnel to be asked which sort of newcomer they were. */}
          <Button
            asChild
            size="sm"
            variant="outline"
            className="hidden border-hairline-strong sm:inline-flex"
          >
            <Link href={SIGN_IN_PATH}>
              <LogIn className="size-3.5" aria-hidden="true" />
              {consoleCta.label}
            </Link>
          </Button>
          <Button
            asChild
            size="sm"
            className="hidden bg-brand text-brand-foreground hover:bg-brand/90 sm:inline-flex"
          >
            <a href={contact.primaryCta.href}>{contact.primaryCta.label}</a>
          </Button>
          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            aria-expanded={menuOpen}
            aria-controls="mobile-nav"
            aria-label={menuOpen ? "Close menu" : "Open menu"}
            className="inline-flex size-9 items-center justify-center rounded-md border border-hairline text-foreground transition-colors hover:bg-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand lg:hidden"
          >
            {menuOpen ? (
              <X className="size-4.5" aria-hidden="true" />
            ) : (
              <Menu className="size-4.5" aria-hidden="true" />
            )}
          </button>
        </div>
      </div>

      <div
        id="mobile-nav"
        hidden={!menuOpen}
        className="border-t border-hairline bg-background/95 backdrop-blur-xl lg:hidden"
      >
        <nav aria-label="Mobile" className="mx-auto max-w-7xl px-6 py-5">
          <ul className="flex flex-col">
            {nav.map((item) => (
              <li key={item.href}>
                <a
                  href={item.href}
                  onClick={() => setMenuOpen(false)}
                  className="flex items-center justify-between border-b border-hairline py-3.5 text-base text-foreground/90 transition-colors hover:text-brand"
                >
                  {item.label}
                  <span aria-hidden="true" className="eyebrow text-muted-foreground">
                    {item.href.replace("/#", "")}
                  </span>
                </a>
              </li>
            ))}
          </ul>
          <div className="mt-6 flex items-center justify-between gap-4">
            <span className="eyebrow text-muted-foreground">Theme</span>
            <ThemeToggle />
          </div>
          {/* The menu is the only place these reach a phone, which is the one device
              where the haptics half of them is real. */}
          <div className="mt-3 flex items-center justify-between gap-4">
            <span className="eyebrow text-muted-foreground">Feedback</span>
            <FeedbackToggle />
          </div>
          <Button
            asChild
            className="mt-5 w-full bg-brand text-brand-foreground hover:bg-brand/90"
          >
            <a href={contact.primaryCta.href} onClick={() => setMenuOpen(false)}>
              {contact.primaryCta.label}
            </a>
          </Button>
          {/* The header chip is `sm:inline-flex`, so on a phone this menu is the only
              way back in. Carries the sentence the chip has no room for — the label
              alone assumes the reader knows JUTSU has a console to return to. */}
          <Button
            asChild
            variant="outline"
            className="mt-3 w-full border-hairline-strong"
          >
            <Link href={SIGN_IN_PATH} onClick={() => setMenuOpen(false)}>
              <LogIn className="size-4" aria-hidden="true" />
              {consoleCta.label}
            </Link>
          </Button>
          <p className="mt-2 text-center text-xs text-muted-foreground">
            {consoleCta.description}
          </p>
        </nav>
      </div>

      <ScrollProgress />
    </header>
  );
}
