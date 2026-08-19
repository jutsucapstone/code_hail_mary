"use client";

import { useEffect, useState } from "react";
import { Menu, X } from "lucide-react";

import { Logo, Wordmark } from "@/components/site/logo";
import { ScrollProgress } from "@/components/site/scroll-progress";
import { ThemeToggle } from "@/components/site/theme-toggle";
import { Button } from "@/components/ui/button";
import { contact, nav, siteConfig } from "@/lib/content";
import { cn } from "@/lib/utils";

export function SiteHeader() {
  const [scrolled, setScrolled] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);

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
        "relative transition-[background-color,border-color,backdrop-filter] duration-300",
        scrolled || menuOpen
          ? "border-b border-hairline bg-background/80 backdrop-blur-xl"
          : "border-b border-transparent bg-transparent",
      )}
    >
      <div className="mx-auto flex h-16 w-full max-w-7xl items-center justify-between gap-6 px-6 lg:h-18 lg:px-8">
        <a
          href="#hero"
          className="group flex items-center gap-2.5 rounded-md focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand"
          aria-label={`${siteConfig.name} — home`}
        >
          <Logo
            priority
            className="h-7 w-7 shrink-0 transition-transform duration-300 group-hover:scale-105"
          />
          <Wordmark className="w-[4.75rem]" />
        </a>

        <nav aria-label="Primary" className="hidden lg:block">
          <ul className="flex items-center gap-1">
            {nav.map((item) => (
              <li key={item.href}>
                <a
                  href={item.href}
                  className="rounded-md px-3 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                >
                  {item.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <div className="flex items-center gap-2">
          <ThemeToggle className="hidden sm:inline-flex" />
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
                    {item.href.replace("#", "")}
                  </span>
                </a>
              </li>
            ))}
          </ul>
          <div className="mt-6 flex items-center justify-between gap-4">
            <span className="eyebrow text-muted-foreground">Theme</span>
            <ThemeToggle />
          </div>
          <Button
            asChild
            className="mt-5 w-full bg-brand text-brand-foreground hover:bg-brand/90"
          >
            <a href={contact.primaryCta.href} onClick={() => setMenuOpen(false)}>
              {contact.primaryCta.label}
            </a>
          </Button>
        </nav>
      </div>

      <ScrollProgress />
    </header>
  );
}
