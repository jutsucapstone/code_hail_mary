import { afterEach, describe, expect, it, vi } from "vitest";

import config from "./next.config";

/**
 * The security headers the deployment actually sends, asserted against the config that
 * produces them.
 *
 * These were verified against production by hand and four of the five were already
 * arriving; `Strict-Transport-Security` was the one that was not, and a header nobody
 * checks is a header that quietly stops being sent. This is the cheapest place to pin
 * it: `headers()` is a pure async function, so the assertion needs no browser, no
 * build and no network.
 */

type HeaderEntry = { key: string; value: string };
type Rule = { source: string; headers: HeaderEntry[] };

async function rulesFor(nodeEnv: string): Promise<Rule[]> {
  vi.stubEnv("NODE_ENV", nodeEnv);
  return (await config.headers!()) as unknown as Rule[];
}

/** The always-on rule: the five headers every path gets. */
async function headersFor(nodeEnv: string): Promise<HeaderEntry[]> {
  const rules = await rulesFor(nodeEnv);
  const universal = rules.find((rule) => rule.source === "/:path*");
  expect(universal, "the /:path* rule carrying the non-CSP headers").toBeDefined();
  return universal!.headers;
}

/** The one policy, which every path now receives. */
async function cspFor(nodeEnv: string): Promise<string> {
  const rules = await rulesFor(nodeEnv);
  const universal = rules.find((rule) => rule.source === "/:path*");
  const csp = universal!.headers.find((h) => h.key === "Content-Security-Policy");
  expect(csp, "a Content-Security-Policy header").toBeDefined();
  return csp!.value;
}

function directive(csp: string, name: string): string {
  const found = csp
    .split(";")
    .map((part) => part.trim())
    .find((part) => part === name || part.startsWith(`${name} `));
  expect(found, `a ${name} directive in ${csp}`).toBeDefined();
  return found!;
}

function valueOf(headers: HeaderEntry[], key: string): string | undefined {
  return headers.find((h) => h.key.toLowerCase() === key.toLowerCase())?.value;
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("strict transport security", () => {
  it("is sent on a production build", async () => {
    const headers = await headersFor("production");

    expect(valueOf(headers, "Strict-Transport-Security")).toBe(
      "max-age=31536000; includeSubDomains",
    );
  });

  it("is a year, which is long enough to matter and short enough to correct", async () => {
    const value = valueOf(await headersFor("production"), "Strict-Transport-Security")!;
    const maxAge = Number(/max-age=(\d+)/.exec(value)![1]);

    expect(maxAge).toBeGreaterThanOrEqual(31536000);
  });

  it("covers subdomains, which was checked against the live domain rather than assumed", async () => {
    // `jutsu.co.in` answers 200 and `www.jutsu.co.in` answers 301, both over HTTPS, and
    // no other hostname resolves. That is what makes this directive safe to assert.
    expect(valueOf(await headersFor("production"), "Strict-Transport-Security")).toContain(
      "includeSubDomains",
    );
  });

  it("does not claim preload", async () => {
    // Preload is a submission to a list browsers ship, and it is slow and awkward to
    // reverse. That is a decision about the domain, not about this file — so it must
    // not appear here by accident.
    expect(valueOf(await headersFor("production"), "Strict-Transport-Security")).not.toContain(
      "preload",
    );
  });

  it("is absent from the dev server, which serves plain HTTP", async () => {
    const headers = await headersFor("development");

    expect(valueOf(headers, "Strict-Transport-Security")).toBeUndefined();
  });
});

describe("the headers that were already arriving", () => {
  it("keeps every one of them in both environments", async () => {
    for (const environment of ["production", "development"]) {
      const headers = await headersFor(environment);

      expect(valueOf(headers, "X-Content-Type-Options")).toBe("nosniff");
      expect(valueOf(headers, "X-Frame-Options")).toBe("SAMEORIGIN");
      expect(valueOf(headers, "Referrer-Policy")).toBe("strict-origin-when-cross-origin");
      expect(valueOf(headers, "Permissions-Policy")).toBe(
        "camera=(), microphone=(), geolocation=()",
      );
    }
  });

  it("applies to every path, not just the marketing pages", async () => {
    const rules = await rulesFor("production");

    expect(rules[0].source).toBe("/:path*");
  });
});

describe("the content security policy", () => {
  it("is one policy on one rule, because a per-route CSP does not survive soft navigation", async () => {
    // This was two rules — strict everywhere, unpkg on /handover. A CSP belongs to the
    // DOCUMENT, not the route, and /handover is reached by `next/link` from the KT shell,
    // so the page inherited the entry document's policy and the scene was blocked on the
    // normal in-app path while working on a hard load. Protection that depends on how the
    // reader arrived is worse than none.
    const rules = await rulesFor("production");
    const withCsp = rules.filter((rule) =>
      rule.headers.some((header) => header.key === "Content-Security-Policy"),
    );

    expect(withCsp).toHaveLength(1);
    expect(withCsp[0].source).toBe("/:path*");
  });

  it("closes the routes that do not need inline script", async () => {
    // Strict regardless of the `'unsafe-inline'` compromise; each closes an injection
    // route on its own.
    const csp = await cspFor("production");

    expect(directive(csp, "object-src")).toBe("object-src 'none'");
    expect(directive(csp, "base-uri")).toBe("base-uri 'self'");
    expect(directive(csp, "form-action")).toBe("form-action 'self'");
    expect(directive(csp, "frame-src")).toBe("frame-src 'none'");
  });

  it("confines network calls to this origin and Cloud Storage", async () => {
    // Narrow because every API call goes through the same-origin proxy at /api/jutsu/*.
    // unpkg was here only because the Spline viewer was loaded from it; the viewer is
    // vendored into `public/spline/` now, so the origin is gone.
    //
    // The storage origin is NOT optional and is not a convenience: a Knowledge Basket
    // upload PUTs its bytes straight to Cloud Storage under a signed URL (ADR 0020).
    // Without this exact origin the browser blocks the request and the feature is dead in
    // production while every test still passes — a scripted `fetch` has no CSP. That is
    // precisely why it is pinned here rather than left to be noticed.
    expect(directive(await cspFor("production"), "connect-src")).toBe(
      "connect-src 'self' https://storage.googleapis.com",
    );
  });

  it("lets an uploaded image and recording render from their signed URL", async () => {
    // A preview fetches the object directly rather than proxying it through Cloud Run,
    // so `img-src` and `media-src` need the same origin `connect-src` does.
    const csp = await cspFor("production");

    expect(directive(csp, "img-src")).toContain("https://storage.googleapis.com");
    expect(directive(csp, "media-src")).toContain("https://storage.googleapis.com");
  });

  it("names every network origin exactly, never a scheme or a wildcard", async () => {
    // The value of a narrow connect-src is that injected script has nowhere to
    // exfiltrate to. A bare `https:` or a `*.googleapis.com` gives that away for nothing.
    //
    // Asserted over the directive's TOKENS rather than as a substring of the policy: the
    // obvious `not.toContain("https:")` is satisfied by any `https://…` source, so it
    // passes whatever the policy says and proves nothing. That is the version this test
    // shipped with first, and preflight caught it.
    const sources = directive(await cspFor("production"), "connect-src")
      .split(/\s+/)
      .slice(1);

    expect(sources).toEqual(["'self'", "https://storage.googleapis.com"]);
    for (const source of sources) {
      expect(source, `${source} is a wildcard`).not.toContain("*");
      // A scheme-only source (`https:`) permits every host that speaks it.
      expect(source, `${source} is a bare scheme`).not.toMatch(/^https?:$/);
    }
  });

  it("allows no third-party script origin at all", async () => {
    // The viewer was the only one, and it is vendored. `'unsafe-inline'` remains a
    // documented Next 16 limitation (nonces disable static generation, CDN caching and
    // PPR); what this pins is that no OFF-ORIGIN host may execute script here, which is
    // the half that governs supply chain.
    const scriptSrc = directive(await cspFor("production"), "script-src");

    expect(scriptSrc).toBe("script-src 'self' 'unsafe-inline'");
    expect(scriptSrc).not.toContain("//");
  });

  it("agrees with X-Frame-Options rather than contradicting it", async () => {
    // Two headers that disagree about framing is how one of them silently stops being
    // the answer, depending on which browser is reading.
    expect(directive(await cspFor("production"), "frame-ancestors")).toBe(
      "frame-ancestors 'self'",
    );
    expect(valueOf(await headersFor("production"), "X-Frame-Options")).toBe("SAMEORIGIN");
  });

  it("allows eval in development only, because React needs it there", async () => {
    expect(await cspFor("development")).toContain("'unsafe-eval'");
    expect(await cspFor("production")).not.toContain("'unsafe-eval'");
  });

  it("upgrades insecure requests on a production build only", async () => {
    expect(await cspFor("production")).toContain("upgrade-insecure-requests");
    // The dev server is plain HTTP; upgrading there would make it unreachable.
    expect(await cspFor("development")).not.toContain("upgrade-insecure-requests");
  });
});
