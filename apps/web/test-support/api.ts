import { vi } from "vitest";

/**
 * Scripting the one `fetch` that `lib/api.ts` makes.
 *
 * The API client is a single `fetch` behind `call()`, so stubbing it is the whole seam:
 * no service worker, no API process, no database, and — critically — no provider. A
 * frontend test that reached Vertex would bill CI per assertion.
 *
 * Extracted from `evidence-search.test.tsx`, which had it inline. A second test file was
 * about to copy it, and two copies of a mock drift in exactly the way that makes one
 * suite assert against a response shape the other has already moved on from.
 */

export type Json = Record<string, unknown>;

/** One scripted response. `status` drives `ok`, the way a real Response does. */
export interface ScriptedResponse {
  status: number;
  body: Json | null;
}

/**
 * Replace `fetch` with a mock that answers the given responses in order.
 *
 * Returns the mock, because what a test usually needs to assert is not the render but
 * the *request*: which URL, which method, and which fields the browser actually sent.
 */
export function scriptFetch(...responses: ScriptedResponse[]) {
  const fetchMock = vi.fn();
  for (const { status, body } of responses) {
    fetchMock.mockResolvedValueOnce({
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    });
  }
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** A `fetch` that never resolves, plus the handle that releases it. For loading states. */
export function pendingFetch(resolveWith: ScriptedResponse) {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const fetchMock = vi.fn().mockReturnValue(
    gate.then(() => ({
      ok: resolveWith.status >= 200 && resolveWith.status < 300,
      status: resolveWith.status,
      json: async () => resolveWith.body,
    })),
  );
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, release };
}

type FetchMock = ReturnType<typeof vi.fn>;

/** The URL of the nth call. */
export function calledUrl(fetchMock: FetchMock, index = 0): string {
  return String(fetchMock.mock.calls[index][0]);
}

/** The method of the nth call. */
export function calledMethod(fetchMock: FetchMock, index = 0): string | undefined {
  return (fetchMock.mock.calls[index][1] as RequestInit | undefined)?.method;
}

/**
 * The parsed JSON body of the nth call.
 *
 * This is how "the browser never sends a tenant field" is checked rather than asserted
 * in a comment: read what was sent and look at its keys.
 */
export function sentBody(fetchMock: FetchMock, index = 0): Json {
  const init = fetchMock.mock.calls[index][1] as RequestInit;
  return JSON.parse(String(init.body)) as Json;
}

/** The error envelope every 4xx and 5xx carries (§15). */
export function envelope(code: string, message: string, requestId = "req-abc"): Json {
  return { error: { code, message, details: {} }, request_id: requestId };
}

/**
 * A `Capabilities` payload, as `GET /v1/me` returns it.
 *
 * Shells fetch this before rendering anything, so almost every component test needs one.
 */
export function capabilities(overrides: Json = {}): Json {
  return {
    user_id: "44444444-4444-4444-8444-444444444444",
    org_id: "55555555-5555-4555-8555-555555555555",
    jutsu_id: "JUTSU-ADM-9HXPNFG8",
    role: "owner",
    permissions: [
      "org:read",
      "member:read",
      "member:invite",
      "integration:read",
      "integration:connect",
      "integration:revoke",
      "integration:self_manage",
      "profile:self_read",
      "retrieval:query",
    ],
    ...overrides,
  };
}
