import { describe, expect, it } from "vitest";

import { CONTACT_EMAIL, footerNav, siteConfig } from "@/lib/content";

/**
 * The addresses and destinations the marketing site publishes.
 *
 * This exists because of a real incident rather than a style preference. Every CTA, the
 * footer and both legal pages published `hello@jutsu.dev` — a domain registered to
 * somebody else, resolving to Cloudflare with live Protonmail MX records, while this
 * project's DNS is GoDaddy and serves `jutsu.co.in`. Customers' privacy and legal
 * enquiries, which contain personal data by definition, were addressed to a mailbox
 * nobody here controls.
 *
 * So the property under test is narrow and absolute: **nothing this site publishes may
 * point at a domain the deployment does not own.**
 */

/** The domain this deployment actually owns and serves from. */
const OWNED = "jutsu.co.in";

/** Every href the site publishes, from the one module that defines them. */
function everyHref(): string[] {
  const hrefs: string[] = [];
  const walk = (value: unknown): void => {
    if (typeof value === "string") {
      if (value.startsWith("mailto:") || value.startsWith("http")) hrefs.push(value);
      return;
    }
    if (Array.isArray(value)) {
      value.forEach(walk);
      return;
    }
    if (value && typeof value === "object") {
      Object.values(value).forEach(walk);
    }
  };
  walk(siteConfig);
  walk(footerNav);
  return hrefs;
}

describe("the published contact address", () => {
  it("is on the domain this deployment owns", () => {
    expect(CONTACT_EMAIL.split("@")[1]).toBe(OWNED);
  });

  it("is never on jutsu.dev, which belongs to somebody else", () => {
    // Named explicitly rather than only checked structurally: the next person to reach
    // for a contact address should meet the reason, not just a rule.
    expect(CONTACT_EMAIL).not.toContain("jutsu.dev");
  });

  it("is not a no-reply address dressed as a contact", () => {
    // Publishing `noreply@` as the place to send a privacy request is a way of not
    // having a contact address while appearing to.
    expect(CONTACT_EMAIL.toLowerCase()).not.toMatch(/^(no-?reply|do-?not-?reply)@/);
  });
});

describe("every destination the site publishes", () => {
  it("points at an owned domain or a relative path", () => {
    for (const href of everyHref()) {
      const host = href.startsWith("mailto:")
        ? (href.split("@")[1] ?? "").split("?")[0]
        : new URL(href).hostname;
      // `siteConfig.url` falls back to the dev origin when NEXT_PUBLIC_SITE_URL is
      // unset, which is what a test run sees. It is documented in `content.ts` as a
      // value that must not ship, and the build sets it — so it is skipped here rather
      // than treated as a leak.
      if (host === "localhost" || host === "127.0.0.1") continue;
      // Third-party links the site legitimately makes (a status page, a social
      // profile) would be added here deliberately. There are none today, and that is
      // the point: an unexplained new host is a review conversation.
      expect(host.endsWith(OWNED), `${href} leaves ${OWNED}`).toBe(true);
    }
  });

  it("publishes at least one mailto, so this test is not vacuously green", () => {
    // Without this, deleting every contact link would make the sweep above pass.
    expect(everyHref().some((href) => href.startsWith("mailto:"))).toBe(true);
  });
});
