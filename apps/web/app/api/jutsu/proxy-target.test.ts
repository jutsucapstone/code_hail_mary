import { describe, expect, it } from "vitest";

import { upstreamUrl } from "@/app/api/jutsu/[...path]/route";

/**
 * The one thing this proxy must never do: address a host the deployment did not choose.
 *
 * It forwards every request header, `cookie` included, so an origin it can be talked out
 * of is a session-exfiltration primitive rather than an SSRF curiosity. The cases below
 * are the two shapes that actually moved the origin when the target was built as
 * `new URL("/" + path.join("/"), API_ORIGIN)`, and both arrive through the front door:
 * Next decodes catch-all segments, so `%5C` reaches this code as a real backslash and an
 * empty segment reaches it as an empty string.
 */

const ORIGIN = "http://api.internal:8000";

describe("the upstream origin", () => {
  it("addresses the configured API for an ordinary path", () => {
    const target = upstreamUrl(["v1", "me"], "", ORIGIN);

    expect(target.origin).toBe(ORIGIN);
    expect(target.pathname).toBe("/v1/me");
  });

  it("keeps the query string the browser sent", () => {
    const target = upstreamUrl(["v1", "employees"], "?cursor=abc&limit=50", ORIGIN);

    expect(target.search).toBe("?cursor=abc&limit=50");
    expect(target.origin).toBe(ORIGIN);
  });

  it.each([
    ["a decoded backslash, which WHATWG parses as a slash", ["\\evil.example", "v1", "me"]],
    ["an empty leading segment, making the rest protocol-relative", ["", "evil.example", "v1"]],
    ["a decoded double slash inside one segment", ["//evil.example", "v1"]],
    ["a whole URL in a segment", ["http://evil.example/v1", "me"]],
  ])("cannot be moved by %s", (_why, path) => {
    const target = upstreamUrl(path, "", ORIGIN);

    expect(target.origin).toBe(ORIGIN);
    expect(target.host).not.toContain("evil.example");
  });

  it("escapes a separator inside a segment rather than letting it split the path", () => {
    const target = upstreamUrl(["v1", "kt", "AB/CD"], "", ORIGIN);

    expect(target.pathname).toBe("/v1/kt/AB%2FCD");
  });

  it("leaves the identifiers the API actually uses untouched", () => {
    const target = upstreamUrl(
      ["v1", "employees", "3f2504e0-4f89-11d3-9a0c-0305e82c3301", "connections"],
      "",
      ORIGIN,
    );

    expect(target.pathname).toBe(
      "/v1/employees/3f2504e0-4f89-11d3-9a0c-0305e82c3301/connections",
    );
  });
});
