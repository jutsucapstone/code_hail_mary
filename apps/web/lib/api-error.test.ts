import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import { classifyApiError, isRetryable, needsSignIn } from "@/lib/api-error";

function apiError(status: number, message = "Refused.", requestId = "req-1") {
  return new ApiError(status, {
    error: { code: "whatever", message, details: {} },
    request_id: requestId,
  });
}

describe("classification", () => {
  it("reads the status, not the code", () => {
    // The same code under two statuses must classify by status: §15 commits to the
    // status, while `code` is free to gain new values inside one.
    expect(classifyApiError(apiError(403)).kind).toBe("denied");
    expect(classifyApiError(apiError(429)).kind).toBe("throttled");
  });

  it("maps each documented status to its own kind", () => {
    expect(classifyApiError(apiError(401)).kind).toBe("auth");
    expect(classifyApiError(apiError(403)).kind).toBe("denied");
    expect(classifyApiError(apiError(404)).kind).toBe("missing");
    expect(classifyApiError(apiError(422)).kind).toBe("invalid");
    expect(classifyApiError(apiError(429)).kind).toBe("throttled");
    expect(classifyApiError(apiError(503)).kind).toBe("unavailable");
    expect(classifyApiError(apiError(500)).kind).toBe("other");
  });

  it("writes its own sentence for a 401 rather than forwarding the API's", () => {
    // The API's 401 message is deliberately uninformative — it must not confirm whether
    // an account exists. Forwarding it would tell the reader nothing actionable.
    const failure = classifyApiError(apiError(401, "Not authenticated."));
    expect(failure.message).toMatch(/sign in again/i);
  });

  it("forwards the API's message for every failure the reader can act on", () => {
    expect(classifyApiError(apiError(422, "Query too long.")).message).toBe("Query too long.");
    expect(classifyApiError(apiError(429, "Slow down.")).message).toBe("Slow down.");
  });

  it("keeps the request id, which identifies a request and not a person", () => {
    expect(classifyApiError(apiError(500, "Boom.", "req-xyz")).requestId).toBe("req-xyz");
  });

  it("survives a rejection that is not an ApiError at all", () => {
    // A transport fault — offline, DNS, a proxy that answered HTML — never reaches the
    // envelope. It still has to render.
    const failure = classifyApiError(new TypeError("Failed to fetch"));
    expect(failure.kind).toBe("other");
    expect(failure.message).toBeTruthy();
    expect(failure.status).toBeUndefined();
  });
});

describe("what the reader is offered", () => {
  it("does not offer a retry for anything that will answer identically", () => {
    // A "Try again" that cannot work teaches people the control does nothing, and then
    // they do not press the one that would have helped.
    for (const status of [401, 403, 404, 422]) {
      expect(isRetryable(classifyApiError(apiError(status)))).toBe(false);
    }
  });

  it("offers a retry where a second attempt could genuinely succeed", () => {
    for (const status of [429, 503, 500]) {
      expect(isRetryable(classifyApiError(apiError(status)))).toBe(true);
    }
    expect(isRetryable(classifyApiError(new TypeError("offline")))).toBe(true);
  });

  it("singles out the one failure that means leave the page", () => {
    expect(needsSignIn(classifyApiError(apiError(401)))).toBe(true);
    // A 403 must not: the session is fine, and sending someone to sign in again to fix a
    // permission problem is a loop they cannot exit.
    expect(needsSignIn(classifyApiError(apiError(403)))).toBe(false);
  });
});
