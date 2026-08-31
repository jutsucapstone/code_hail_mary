import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import { createQueryClient, queryKeys, shouldRetry } from "@/lib/query";

/**
 * The retry and network policy, tested directly rather than through a component.
 *
 * Component tests build their own `QueryClient` with retries off, so that a test asserting
 * an error state does not sit through a backoff. That is the right trade for them and it
 * means they cannot see this file's defaults at all — the paused-query bug below reached a
 * browser precisely because every test in the suite had overridden the option that caused
 * it. Anything that only the production client configures is asserted here.
 */

function apiError(status: number) {
  return new ApiError(status, {
    error: { code: "x", message: "no", details: {} },
    request_id: "req-1",
  });
}

describe("network mode", () => {
  it("is 'always', so a request is attempted rather than parked in advance", () => {
    // Under the default "online", a query that starts while the browser believes it is
    // offline is paused, and a paused query stays `pending` for ever — it never becomes
    // an error, so no surface can render one. Every request here goes to a same-origin
    // proxy, so `navigator.onLine` was never evidence about whether it could succeed.
    //
    // Note this does NOT make retries unconditional: the library gates *continuing* a
    // retry on document focus in every mode, so a failure in a background tab parks until
    // the tab is looked at again. That is intended, and is not what this option controls.
    const defaults = createQueryClient().getDefaultOptions();

    expect(defaults.queries?.networkMode).toBe("always");
    // Mutations too: a write that silently parks is worse, because the reader believes
    // they have saved something.
    expect(defaults.mutations?.networkMode).toBe("always");
  });
});

describe("retry policy", () => {
  it("never replays a mutation", () => {
    // A mutation is a write. Replaying one because the response was slow is how a single
    // invitation becomes three.
    expect(createQueryClient().getDefaultOptions().mutations?.retry).toBe(false);
  });

  it("is installed on the client, not left to the library's default of three", () => {
    expect(createQueryClient().getDefaultOptions().queries?.retry).toBe(shouldRetry);
  });

  it("gives up immediately on a decision the server will repeat", () => {
    // TanStack calls this with the number of failures *so far*, so 1 is the first
    // failure. A 401 must not delay the sign-in redirect behind a backoff, and retrying a
    // 429 is how a rate limit becomes an outage.
    for (const status of [400, 401, 403, 404, 409, 422, 429]) {
      expect(shouldRetry(1, apiError(status))).toBe(false);
    }
  });

  it("retries a server error and a transport fault, once", () => {
    expect(shouldRetry(0, apiError(500))).toBe(true);
    expect(shouldRetry(0, apiError(503))).toBe(true);
    expect(shouldRetry(0, new TypeError("Failed to fetch"))).toBe(true);

    // …and only once. An unreachable API should reach the error state promptly, not after
    // three backoffs.
    expect(shouldRetry(1, apiError(500))).toBe(false);
    expect(shouldRetry(1, new TypeError("Failed to fetch"))).toBe(false);
  });
});

describe("caching", () => {
  it("keeps an answer fresh long enough to survive a navigation", () => {
    // The reason the library is here: both shells used to re-fetch `GET /v1/me` on every
    // move between sections.
    const staleTime = createQueryClient().getDefaultOptions().queries?.staleTime;
    expect(staleTime).toBeGreaterThan(0);
  });

  it("does not cache so long that a role change goes unnoticed", () => {
    const staleTime = createQueryClient().getDefaultOptions().queries?.staleTime as number;
    expect(staleTime).toBeLessThanOrEqual(60_000);
  });
});

describe("query keys", () => {
  it("scopes a per-employee key by that employee", () => {
    // Two employees' identity lists must not share a cache entry.
    expect(queryKeys.employeeIdentities("a")).not.toEqual(queryKeys.employeeIdentities("b"));
  });

  it("distinguishes employee pages by cursor and search", () => {
    expect(queryKeys.employees({ cursor: "c1" })).not.toEqual(queryKeys.employees({ cursor: "c2" }));
    expect(queryKeys.employees({ q: "ann" })).not.toEqual(queryKeys.employees({ q: "bob" }));
  });

  it("treats a missing cursor and an explicit null as the same page", () => {
    // Otherwise the first page is cached twice and the second render refetches it.
    expect(queryKeys.employees({})).toEqual(queryKeys.employees({ cursor: null, q: null }));
  });

  it("nests the caller's own resources under the identity key", () => {
    // So that signing out can drop everything about a person with one prefix.
    expect(queryKeys.myProfile[0]).toBe(queryKeys.me[0]);
    expect(queryKeys.myIdentities[0]).toBe(queryKeys.me[0]);
  });
});
