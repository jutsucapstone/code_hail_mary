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

async function headersFor(nodeEnv: string): Promise<HeaderEntry[]> {
  vi.stubEnv("NODE_ENV", nodeEnv);
  const rules = await config.headers!();
  expect(rules).toHaveLength(1);
  expect(rules[0].source).toBe("/:path*");
  return rules[0].headers as HeaderEntry[];
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
    const rules = await config.headers!();

    expect(rules[0].source).toBe("/:path*");
  });
});
