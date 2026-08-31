import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "@/lib/api";

/**
 * Server-state caching for the consoles.
 *
 * TanStack Query is named in the fixed stack (§5) and was never installed. Every console
 * surface hand-rolled `useState` + `useEffect` + a `cancelled` flag instead, which meant
 * each of them re-fetched `GET /v1/me` from scratch on every navigation between admin
 * sections — the shell unmounts, the effect runs again, and the reader watches a skeleton
 * for a round trip that answered the same question thirty seconds ago. §44 asks for
 * request deduplication; this is where it comes from.
 *
 * **Caching here is never an authorization decision.** A cached `Capabilities` decides
 * which nav entries render, and rendering a door is not opening it: every endpoint
 * re-checks server-side, so a stale permission set shows a link that then 403s. That is
 * the same courtesy/enforcement split the shells already document. It does *not* license
 * caching anything that is itself a grant — ACL principals resolve inside the SQL per
 * request precisely so a revocation lands on the next query (ADR 0011), and putting any
 * of that behind a `staleTime` would reintroduce the bug that ADR exists to prevent.
 */

/**
 * Statuses that will answer identically however many times they are asked.
 *
 * The library's default is three retries with backoff, which is wrong for all of these
 * and actively harmful for two. A 401 is the signal to send someone to sign-in, and
 * retrying it delays that redirect by several seconds while the reader looks at a
 * spinner. A 429 means a budget is already spent, and retrying is how a rate limit
 * becomes an outage. A 403 and a 422 are decisions, not weather.
 */
const TERMINAL_STATUSES = new Set([400, 401, 403, 404, 409, 422, 429]);

/** Retry transport faults and genuine server errors once. Never a decision. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && TERMINAL_STATUSES.has(error.status)) return false;
  return failureCount < 1;
}

/**
 * Query keys, in one place.
 *
 * Written as a const map rather than inline arrays so that an invalidation and the query
 * it is meant to invalidate cannot disagree about the key — a typo in one of two string
 * arrays produces a mutation that appears to succeed and a list that never updates, and
 * nothing fails.
 */
export const queryKeys = {
  /** `GET /v1/me` — the caller's identity, role and permission set. */
  me: ["me"] as const,
  /** `GET /v1/orgs/current` — organisation profile and member counts. */
  organisation: ["organisation"] as const,
  /** `GET /v1/me/profile` — the caller's own employee profile. */
  myProfile: ["me", "profile"] as const,
  /** `GET /v1/me/identities` — the caller's own linked source identities. */
  myIdentities: ["me", "identities"] as const,
  /** `GET /v1/employees/{id}/identities` — one employee's links. */
  employeeIdentities: (userId: string) => ["employees", userId, "identities"] as const,
  /** `GET /v1/employees` — one page of the employee list. */
  employees: (params: { cursor?: string | null; q?: string | null }) =>
    ["employees", { cursor: params.cursor ?? null, q: params.q ?? null }] as const,
} as const;

/**
 * How long a console answer stays fresh.
 *
 * Thirty seconds, which is long enough that moving between admin sections is instant and
 * short enough that a role change is visible without a reload. `refetchOnWindowFocus`
 * stays at its default of `true`, so a tab left open overnight re-checks when it is
 * looked at again rather than rendering yesterday's permission set.
 */
const STALE_MS = 30_000;

/**
 * Attempt the request rather than deciding in advance that it cannot succeed.
 *
 * Under the default `"online"`, `canStart()` is `onlineManager.isOnline()`, so a query
 * that begins while the browser believes it is offline is *paused* — and a paused query
 * holds `status: "pending"` for ever. It never becomes an error, so nothing downstream can
 * render one, and the console sits on its loading skeleton with no message and no way out.
 * That is the blank-screen dead end §34 exists to rule out.
 *
 * `"always"` is honest for this app specifically: every request goes to a **same-origin**
 * proxy at `/api/jutsu/...`, and `navigator.onLine` only reports whether the machine has
 * any network interface at all. It was never evidence about whether *this* request could
 * succeed, and a dropped connection should surface as the same visible, classified,
 * retryable failure as any other transport fault — which is why `classifyApiError` handles
 * a rejection carrying no envelope.
 *
 * **This does not make retries unconditional, and that is not a bug.** `retryer.ts` gates
 * *continuing* a retry on `focusManager.isFocused()` in every network mode, so a query
 * that fails in a background tab parks at `fetchStatus: "paused"` until the tab is looked
 * at again, then finishes. Worth knowing before diagnosing it twice: driving the console
 * from an unfocused automation tab shows a permanent skeleton that has nothing to do with
 * this setting. Focus the tab and the retry completes and the error renders.
 */
const NETWORK_MODE = "always" as const;

/**
 * One client per browser session.
 *
 * Built by a factory rather than exported as a module singleton: on the server a module
 * singleton is shared by every concurrent request, so one tenant's cached organisation
 * would be handed to the next caller. `QueryProvider` holds this in state so it is
 * created once per mount and never crosses a request boundary.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: STALE_MS,
        retry: shouldRetry,
        networkMode: NETWORK_MODE,
      },
      mutations: {
        // A mutation is a write. Replaying one because the response was slow is how a
        // single invitation becomes three.
        retry: false,
        networkMode: NETWORK_MODE,
      },
    },
  });
}
