import type { paths } from "@/lib/api-schema";

/**
 * The browser's only route to the API.
 *
 * Every request goes to `/api/jutsu/...` on this origin, which the proxy forwards. That
 * keeps the session cookie first-party and means the API needs no CORS at all — a
 * permissive origin on a multi-tenant API is a tenant-isolation risk, so having none is
 * better than having one configured carefully.
 *
 * Request and response types come from `api-schema.d.ts`, which is generated from the
 * FastAPI OpenAPI document by `make api-types` and checked for staleness in preflight.
 * §4.13 forbids hand-writing them: a hand-maintained copy drifts, and it drifts silently,
 * because TypeScript happily checks against a contract the server stopped honouring.
 */

/** The one error shape the API emits for every 4xx and 5xx (§15). */
export interface ApiErrorEnvelope {
  error: { code: string; message: string; details: Record<string, unknown> };
  request_id: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;

  constructor(status: number, envelope: ApiErrorEnvelope) {
    super(envelope.error.message);
    this.name = "ApiError";
    this.status = status;
    this.code = envelope.error.code;
    this.requestId = envelope.request_id;
  }
}

const CSRF_COOKIE = "__Host-jutsu_csrf";
const CSRF_HEADER = "x-jutsu-csrf";

/**
 * Read the CSRF partner cookie.
 *
 * Deliberately readable by script — that is the mechanism, not an oversight. It is not a
 * credential on its own: without the httpOnly session cookie it authorises nothing, and
 * an attacker on another origin cannot read it to echo it back.
 */
function csrfToken(): string | null {
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${CSRF_COOKIE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}=([^;]*)`),
  );
  return match ? decodeURIComponent(match[1]) : null;
}

async function call<T>(path: string, init: RequestInit): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("content-type", "application/json");

  const token = csrfToken();
  if (token) headers.set(CSRF_HEADER, token);

  const response = await fetch(`/api/jutsu${path}`, {
    ...init,
    headers,
    // Same-origin, so the cookie rides along by default — but stated explicitly, because
    // the whole session design depends on it and a silent default is easy to break.
    credentials: "same-origin",
  });

  if (response.status === 204) return undefined as T;

  const payload: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    // Every 4xx and 5xx carries the envelope. A response that does not is a proxy or
    // infrastructure failure, so it gets a shape the UI can still render rather than an
    // undefined property access.
    const envelope =
      payload && typeof payload === "object" && "error" in payload
        ? (payload as ApiErrorEnvelope)
        : {
            error: {
              code: "unavailable",
              message: "The service is not responding. Please try again.",
              details: {},
            },
            request_id: "unknown",
          };
    throw new ApiError(response.status, envelope);
  }

  return payload as T;
}

type RegisterBody =
  paths["/v1/orgs/register"]["post"]["requestBody"]["content"]["application/json"];
type RegisterResponse =
  paths["/v1/orgs/register"]["post"]["responses"][202]["content"]["application/json"];

type ChallengeBody =
  paths["/v1/auth/request"]["post"]["requestBody"]["content"]["application/json"];
type ChallengeResponse =
  paths["/v1/auth/request"]["post"]["responses"][202]["content"]["application/json"];

type VerifyBody =
  paths["/v1/auth/verify"]["post"]["requestBody"]["content"]["application/json"];
type VerifyResponse =
  paths["/v1/auth/verify"]["post"]["responses"][200]["content"]["application/json"];

type RegisterVerifyBody =
  paths["/v1/orgs/register/verify"]["post"]["requestBody"]["content"]["application/json"];
type RegisterVerifyResponse =
  paths["/v1/orgs/register/verify"]["post"]["responses"][200]["content"]["application/json"];

type MeResponse = paths["/v1/me"]["get"]["responses"][200]["content"]["application/json"];

type OrganisationResponse =
  paths["/v1/orgs/current"]["get"]["responses"][200]["content"]["application/json"];

type EmployeePage =
  paths["/v1/employees"]["get"]["responses"][200]["content"]["application/json"];

type InviteBody =
  paths["/v1/employees/invitations"]["post"]["requestBody"]["content"]["application/json"];
type InviteResponse =
  paths["/v1/employees/invitations"]["post"]["responses"][202]["content"]["application/json"];

type AcceptBody =
  paths["/v1/invitations/accept"]["post"]["requestBody"]["content"]["application/json"];
type AcceptResponse =
  paths["/v1/invitations/accept"]["post"]["responses"][200]["content"]["application/json"];

type SearchBody =
  paths["/v1/search"]["post"]["requestBody"]["content"]["application/json"];
export type SearchResponse =
  paths["/v1/search"]["post"]["responses"][200]["content"]["application/json"];
export type SearchResult = SearchResponse["items"][number];

export const api = {
  registerOrganisation: (body: RegisterBody) =>
    call<RegisterResponse>("/v1/orgs/register", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  requestChallenge: (body: ChallengeBody) =>
    call<ChallengeResponse>("/v1/auth/request", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  verify: (body: VerifyBody) =>
    call<VerifyResponse>("/v1/auth/verify", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Completes a registration and opens the first session.
   *
   * Separate from `verify` on purpose. The two redeem from one challenge namespace but
   * assert different purposes server-side, so a sign-in code cannot create an
   * organisation and a registration code cannot open a session on an existing one.
   */
  completeRegistration: (body: RegisterVerifyBody) =>
    call<RegisterVerifyResponse>("/v1/orgs/register/verify", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Ask the corpus a question. POST because the query is user-authored text and must
   * not reach access logs, proxy logs or `Referer` headers in a URL.
   *
   * `items[].char_start` / `char_end` index the ORIGINAL document, while `text` is the
   * masked body — do not highlight `text` with them. Fetch the span through
   * `/v1/evidence/{chunk_id}` instead.
   *
   * `stats.exhausted` means the search stopped short of `k`, which usually means the
   * caller is not authorized to see `k` documents. It is not an error.
   */
  search: (body: SearchBody) =>
    call<SearchResponse>("/v1/search", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  me: () => call<MeResponse>("/v1/me", { method: "GET" }),

  currentOrganisation: () =>
    call<OrganisationResponse>("/v1/orgs/current", { method: "GET" }),

  employees: (params: { cursor?: string | null; q?: string | null } = {}) => {
    const search = new URLSearchParams();
    if (params.cursor) search.set("cursor", params.cursor);
    if (params.q) search.set("q", params.q);
    const suffix = search.size ? `?${search}` : "";
    return call<EmployeePage>(`/v1/employees${suffix}`, { method: "GET" });
  },

  invite: (body: InviteBody) =>
    call<InviteResponse>("/v1/employees/invitations", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  acceptInvitation: (body: AcceptBody) =>
    call<AcceptResponse>("/v1/invitations/accept", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  logout: () => call<void>("/v1/auth/logout", { method: "POST" }),
};
