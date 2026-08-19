import Link from "next/link";
import { Mail } from "lucide-react";

import { Logo, Wordmark } from "@/components/site/logo";
import { Container } from "@/components/site/section";
import { footerNav, siteConfig } from "@/lib/content";

export function SiteFooter() {
  const year = siteConfig.org.date.split(" ")[1];

  return (
    <footer className="border-t border-hairline bg-surface/30">
      <Container className="py-14 lg:py-16">
        <div className="grid gap-12 lg:grid-cols-[minmax(0,1.5fr)_repeat(3,minmax(0,1fr))]">
          <div className="max-w-sm">
            <div className="flex items-center gap-2.5">
              <Logo className="h-8 w-8" />
              <Wordmark className="w-24" />
            </div>
            <p className="mt-5 text-sm leading-relaxed text-muted-foreground">
              An Enterprise Memory OS built on a temporal graph of people, projects and
              decisions. {siteConfig.tagline}
            </p>
            <a
              href="mailto:hello@jutsu.dev"
              className="mt-6 inline-flex items-center gap-2 rounded-md text-sm text-muted-foreground transition-colors hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
            >
              <Mail aria-hidden="true" className="size-4" />
              hello@jutsu.dev
            </a>
          </div>

          {footerNav.map((group) => (
            <nav key={group.heading} aria-labelledby={`footer-${group.heading}`}>
              <h2
                id={`footer-${group.heading}`}
                className="eyebrow text-muted-foreground/80"
              >
                {group.heading}
              </h2>
              <ul className="mt-5 flex flex-col gap-3">
                {group.links.map((link) => {
                  // Route changes go through the router; in-page hashes and mailto:
                  // must stay plain anchors.
                  const isRoute = link.href.startsWith("/");
                  const cls =
                    "rounded text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand";
                  return (
                    <li key={link.label}>
                      {isRoute ? (
                        <Link href={link.href} className={cls}>
                          {link.label}
                        </Link>
                      ) : (
                        <a href={link.href} className={cls}>
                          {link.label}
                        </a>
                      )}
                    </li>
                  );
                })}
              </ul>
            </nav>
          ))}
        </div>

        <div className="mt-14 flex flex-col gap-4 border-t border-hairline pt-7 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs text-muted-foreground/80">
            © {year} {siteConfig.legalName}. All rights reserved.
          </p>
          <p className="font-mono text-xs text-muted-foreground/80">
            {siteConfig.org.institution} · {siteConfig.org.program}
          </p>
        </div>
      </Container>
    </footer>
  );
}
