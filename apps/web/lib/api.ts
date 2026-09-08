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
type RevokeInvitationResponse =
  paths["/v1/invitations/{invitation_id}/revoke"]["post"]["responses"][200]["content"]["application/json"];

/** What the administrator pasted or uploaded, in whichever form they had it. */
export type BulkSource =
  paths["/v1/employees/invitations/preview"]["post"]["requestBody"]["content"]["application/json"];
/** Every row and what would happen to it. Writes nothing. */
export type BulkPreview =
  paths["/v1/employees/invitations/preview"]["post"]["responses"][200]["content"]["application/json"];
type BulkInviteBody =
  paths["/v1/employees/invitations/bulk"]["post"]["requestBody"]["content"]["application/json"];
/** Every row and what happened to it. */
export type BulkInviteOutcome =
  paths["/v1/employees/invitations/bulk"]["post"]["responses"][202]["content"]["application/json"];
/** One row of either, sharing a vocabulary so the preview and the result read alike. */
export type BulkRowResult = BulkPreview["rows"][number];
export type BulkInviteRow = BulkInviteBody["rows"][number];

type AcceptBody =
  paths["/v1/invitations/accept"]["post"]["requestBody"]["content"]["application/json"];
type AcceptResponse =
  paths["/v1/invitations/accept"]["post"]["responses"][200]["content"]["application/json"];

type SearchBody =
  paths["/v1/search"]["post"]["requestBody"]["content"]["application/json"];
export type Evidence =
  paths["/v1/evidence/{chunk_id}"]["get"]["responses"][200]["content"]["application/json"];

export type EmployeeProfile =
  paths["/v1/me/profile"]["get"]["responses"][200]["content"]["application/json"];
type ProfilePatchBody =
  paths["/v1/me/profile"]["patch"]["requestBody"]["content"]["application/json"];

export type SourceIdentityPage =
  paths["/v1/me/identities"]["get"]["responses"][200]["content"]["application/json"];
export type SourceIdentity = SourceIdentityPage["items"][number];
type LinkBody =
  paths["/v1/employees/{user_id}/identities"]["post"]["requestBody"]["content"]["application/json"];
export type SearchResponse =
  paths["/v1/search"]["post"]["responses"][200]["content"]["application/json"];
export type SearchResult = SearchResponse["items"][number];


export type AuditPage =
  paths["/v1/audit"]["get"]["responses"][200]["content"]["application/json"];
export type AuditEntry = AuditPage["items"][number];
export type JobPage = paths["/v1/jobs"]["get"]["responses"][200]["content"]["application/json"];
export type JobStats =
  paths["/v1/jobs/stats"]["get"]["responses"][200]["content"]["application/json"];
export type SourcePage =
  paths["/v1/sources"]["get"]["responses"][200]["content"]["application/json"];
type SourceSyncQueued =
  paths["/v1/sources/{source_id}/sync"]["post"]["responses"][202]["content"]["application/json"];
export type InvitationPage =
  paths["/v1/invitations"]["get"]["responses"][200]["content"]["application/json"];
type RoleChangeBody =
  paths["/v1/employees/{user_id}/role"]["patch"]["requestBody"]["content"]["application/json"];

/**
 * The role TAXONOMY catalogue: practices, normalized levels, titles and platform codes.
 *
 * Not to be confused with `RoleCatalogue` below, which is the RBAC roles-and-permissions
 * matrix behind `GET /v1/roles`. Two different catalogues describing two different
 * things, and keeping their names apart is the same discipline the feature is built on.
 */
type RoleTaxonomyCatalogue =
  paths["/v1/role-catalogue"]["get"]["responses"][200]["content"]["application/json"];

type RoleAssignmentBody =
  paths["/v1/employees/{user_id}/role-assignment"]["patch"]["requestBody"]["content"]["application/json"];

type RoleAssignment =
  paths["/v1/employees/{user_id}/role-assignment"]["get"]["responses"][200]["content"]["application/json"];
type RoleChangeResponse =
  paths["/v1/employees/{user_id}/role"]["patch"]["responses"][200]["content"]["application/json"];
type OrgRenameBody =
  paths["/v1/orgs/current"]["patch"]["requestBody"]["content"]["application/json"];
type OrgRenameResponse =
  paths["/v1/orgs/current"]["patch"]["responses"][200]["content"]["application/json"];
export type OrgOverview =
  paths["/v1/orgs/current/overview"]["get"]["responses"][200]["content"]["application/json"];
export type SyncSchedule =
  paths["/v1/orgs/current/sync-schedule"]["get"]["responses"][200]["content"]["application/json"];
type SyncScheduleBody =
  paths["/v1/orgs/current/sync-schedule"]["put"]["requestBody"]["content"]["application/json"];
export type RoleCatalogue =
  paths["/v1/roles"]["get"]["responses"][200]["content"]["application/json"];

export type EmployeeConnections =
  paths["/v1/employees/{user_id}/connections"]["get"]["responses"][200]["content"]["application/json"];
export type EmployeeConnection = EmployeeConnections["items"][number];
type IntegrationCatalogue =
  paths["/v1/integrations"]["get"]["responses"][200]["content"]["application/json"];
export type IntegrationEntry = IntegrationCatalogue["items"][number];
export type ConnectionSummary =
  paths["/v1/connections/summary"]["get"]["responses"][200]["content"]["application/json"];
export type ConnectionPolicies =
  paths["/v1/connection-policies"]["get"]["responses"][200]["content"]["application/json"];
type ConnectStarted =
  paths["/v1/me/connections/{provider_id}"]["post"]["responses"][201]["content"]["application/json"];
type SyncQueued =
  paths["/v1/me/connections/{connection_id}/sync"]["post"]["responses"][202]["content"]["application/json"];
type PolicyOut =
  paths["/v1/connection-policies/{provider_id}"]["put"]["responses"][200]["content"]["application/json"];

export type KtAdminPage =
  paths["/v1/kt"]["get"]["responses"][200]["content"]["application/json"];
export type KtAdmin = KtAdminPage["items"][number];
export type KtRecipient =
  paths["/v1/kt/claim"]["post"]["responses"][200]["content"]["application/json"];
export type KtDocumentDetail =
  paths["/v1/kt/{kt_code}/documents/{document_id}"]["get"]["responses"][200]["content"]["application/json"];
export type KtDocumentPage =
  paths["/v1/kt/{kt_code}/documents"]["get"]["responses"][200]["content"]["application/json"];
type KtCreateBody = paths["/v1/kt"]["post"]["requestBody"]["content"]["application/json"];
type KtScopes = paths["/v1/kt/scopes"]["get"]["responses"][200]["content"]["application/json"];

export type AskResponse =
  paths["/v1/ask"]["post"]["responses"][200]["content"]["application/json"];
export type AskCitation = AskResponse["citations"][number];

export type MyKnowledge =
  paths["/v1/me/knowledge"]["get"]["responses"][200]["content"]["application/json"];
export type Departments =
  paths["/v1/departments"]["get"]["responses"][200]["content"]["application/json"];

export type KtInsights =
  paths["/v1/kt/{kt_code}/insights"]["get"]["responses"][200]["content"]["application/json"];
export type KtInsight = KtInsights["items"][number];
export type KtHandoverSummary =
  paths["/v1/kt/{kt_code}/handover-summary"]["get"]["responses"][200]["content"]["application/json"];
type KtInsightSummary =
  paths["/v1/kt/{kt_code}/insights-summary"]["get"]["responses"][200]["content"]["application/json"];

// The KT console (migration 0019): what a recipient asks, keeps and is shown next.
export type KtCopilotTurn =
  paths["/v1/kt/{kt_code}/ask"]["post"]["responses"][200]["content"]["application/json"];
type KtCopilotAskPayload =
  paths["/v1/kt/{kt_code}/ask"]["post"]["requestBody"]["content"]["application/json"];
/** `k` has a server default, which the generator marks as required; the browser never
 *  sets it — how much the copilot reads is the server's decision. Derived, not written. */
type KtCopilotAskBody = Omit<KtCopilotAskPayload, "k"> & Partial<Pick<KtCopilotAskPayload, "k">>;
type KtUpdateBody =
  paths["/v1/kt/{package_id}"]["patch"]["requestBody"]["content"]["application/json"];
export type KtConversationPage =
  paths["/v1/kt/{kt_code}/conversations"]["get"]["responses"][200]["content"]["application/json"];
export type KtConversation = KtConversationPage["items"][number];
export type KtConversationDetail =
  paths["/v1/kt/{kt_code}/conversations/{conversation_id}"]["get"]["responses"][200]["content"]["application/json"];
export type KtMessage = KtConversationDetail["messages"][number];
export type KtStoredCitation = KtMessage["citations"][number];
export type KtBookmarks =
  paths["/v1/kt/{kt_code}/bookmarks"]["get"]["responses"][200]["content"]["application/json"];
export type KtBookmark = KtBookmarks["items"][number];
type KtBookmarkBody =
  paths["/v1/kt/{kt_code}/bookmarks"]["post"]["requestBody"]["content"]["application/json"];
export type KtProgressList =
  paths["/v1/kt/{kt_code}/progress"]["get"]["responses"][200]["content"]["application/json"];
export type KtProgressItem = KtProgressList["items"][number];
export type KtProgressState =
  paths["/v1/kt/{kt_code}/progress/{item_key}"]["put"]["requestBody"]["content"]["application/json"]["state"];
export type KtWorkspace =
  paths["/v1/kt/{kt_code}/workspace"]["get"]["responses"][200]["content"]["application/json"];
export type KtLearningStage = KtWorkspace["learning_path"][number];
export type KtLearningItem = KtLearningStage["items"][number];
export type KtRecommendation = KtWorkspace["recommendations"][number];
export type KtGap = KtWorkspace["gaps"][number];

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

  /**
   * The source span behind one citation.
   *
   * The only correct way to render a highlight. `search()` returns `char_start` and
   * `char_end` that index the ORIGINAL document while its `text` is the masked body, so
   * applying those offsets to that string lands somewhere else — masking changes
   * lengths. This endpoint returns the pair that belong together.
   *
   * A chunk the caller may not read is a 404, not a 403, so this cannot be walked to
   * enumerate documents.
   */
  evidence: (chunkId: string) =>
    call<Evidence>(`/v1/evidence/${encodeURIComponent(chunkId)}`, { method: "GET" }),

  me: () => call<MeResponse>("/v1/me", { method: "GET" }),

  /**
   * The caller's own linked source identities.
   *
   * These are **not** integrations. A source identity is the namespaced provider subject
   * — `{source_system}:{subject}` — that `document_acl` grants are written against, so
   * linking one is what makes documents visible to a person. There is no OAuth here and
   * no content is fetched; that is a different capability which does not exist yet.
   */
  myIdentities: () => call<SourceIdentityPage>("/v1/me/identities", { method: "GET" }),

  /**
   * The caller's own employee profile.
   *
   * **404 is a normal state**, not a fault: an owner or an IT admin is a user with no
   * profile row at all. Callers should render an empty form for it rather than an error.
   */
  myProfile: () => call<EmployeeProfile>("/v1/me/profile", { method: "GET" }),

  /**
   * Create or patch the caller's own profile.
   *
   * A field left out is left alone; a field sent as `null` is cleared. The server takes
   * the user from the session and the organisation from the request's tenant scope, so
   * neither is in this body — and the endpoint rejects unknown fields outright rather
   * than ignoring them.
   */
  updateMyProfile: (body: ProfilePatchBody) =>
    call<EmployeeProfile>("/v1/me/profile", {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  /** One employee's linked identities. Requires `integration:read`. */
  employeeIdentities: (userId: string) =>
    call<SourceIdentityPage>(
      `/v1/employees/${encodeURIComponent(userId)}/identities`,
      { method: "GET" },
    ),

  /**
   * Link a provider subject to an employee. Requires `integration:connect`.
   *
   * The API refuses to let an administrator link a subject to their **own** account, and
   * that refusal is not a permission check — an Owner holds every permission, so gating
   * it on one would make it no refusal at all. Expect a 403 for a self-link and surface
   * it as the deliberate rule it is.
   */
  linkIdentity: (userId: string, body: LinkBody) =>
    call<SourceIdentity>(`/v1/employees/${encodeURIComponent(userId)}/identities`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Revoke a link. Requires `integration:revoke`. The row is kept and marked inactive. */
  revokeIdentity: (userId: string, identityId: string) =>
    call<void>(
      `/v1/employees/${encodeURIComponent(userId)}/identities/${encodeURIComponent(identityId)}`,
      { method: "DELETE" },
    ),

  currentOrganisation: () =>
    call<OrganisationResponse>("/v1/orgs/current", { method: "GET" }),

  /**
   * People in the organisation, optionally narrowed.
   *
   * `level` is the Expert Finder filter and the reason the taxonomy exists: it matches
   * NORMALIZED seniority, so `senior_consultant` returns the Senior Software Engineer,
   * the Audit Senior and the Senior Tax Consultant together. Each row still carries its
   * own real title. `unmapped` is the review queue of people nobody has placed yet.
   */
  employees: (
    params: {
      cursor?: string | null;
      q?: string | null;
      practice?: string | null;
      level?: string | null;
      role_code?: string | null;
      unmapped?: boolean;
    } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.cursor) search.set("cursor", params.cursor);
    if (params.q) search.set("q", params.q);
    if (params.practice) search.set("practice", params.practice);
    if (params.level) search.set("level", params.level);
    if (params.role_code) search.set("role_code", params.role_code);
    if (params.unmapped) search.set("unmapped", "true");
    const suffix = search.size ? `?${search}` : "";
    return call<EmployeePage>(`/v1/employees${suffix}`, { method: "GET" });
  },

  invite: (body: InviteBody) =>
    call<InviteResponse>("/v1/employees/invitations", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * What would happen to each address, without inviting anybody.
   *
   * The whole reason bulk onboarding is two requests: the administrator sees the people
   * who already have accounts and the addresses that are misspelt BEFORE anyone receives
   * mail. Requires `member:invite`, the same permission as inviting one person.
   */
  previewInvitations: (body: BulkSource) =>
    call<BulkPreview>("/v1/employees/invitations/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Invite everyone in `rows`, and report each row's fate.
   *
   * Safe to call again with the same rows: anybody who already got an invitation comes
   * back as `already_invited` rather than receiving a second one, which is what makes
   * "retry the failures" a button rather than a support ticket.
   */
  inviteMany: (body: BulkInviteBody) =>
    call<BulkInviteOutcome>("/v1/employees/invitations/bulk", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  acceptInvitation: (body: AcceptBody) =>
    call<AcceptResponse>("/v1/invitations/accept", {
      method: "POST",
      body: JSON.stringify(body),
    }),


  /**
   * The audit trail. Requires `audit:read`. Actors arrive as opaque ids plus a
   * display JUTSU ID — the API never returns an email here, and the UI must not
   * try to resolve one.
   */
  audit: (
    params: {
      cursor?: string | null;
      action?: string | null;
      outcome?: string | null;
      /** One kind of resource — `kt_package`, say. */
      resource_type?: string | null;
      /** One resource's own history. An opaque id, never a name or an address. */
      resource_id?: string | null;
      limit?: number;
    } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.cursor) search.set("cursor", params.cursor);
    if (params.action) search.set("action", params.action);
    if (params.outcome) search.set("outcome", params.outcome);
    if (params.resource_type) search.set("resource_type", params.resource_type);
    if (params.resource_id) search.set("resource_id", params.resource_id);
    if (params.limit) search.set("limit", String(params.limit));
    const suffix = search.size ? `?${search}` : "";
    return call<AuditPage>(`/v1/audit${suffix}`, { method: "GET" });
  },

  /** Ingestion and embedding jobs. Requires `org:read`. */
  jobs: (params: { cursor?: string | null; state?: string | null } = {}) => {
    const search = new URLSearchParams();
    if (params.cursor) search.set("cursor", params.cursor);
    if (params.state) search.set("state", params.state);
    const suffix = search.size ? `?${search}` : "";
    return call<JobPage>(`/v1/jobs${suffix}`, { method: "GET" });
  },

  jobStats: () => call<JobStats>("/v1/jobs/stats", { method: "GET" }),

  /** Knowledge sources with sync state. Requires `integration:read`. */
  sources: () => call<SourcePage>("/v1/sources", { method: "GET" }),

  /**
   * Queue a walk of one source into the durable job queue. Requires
   * `integration:connect` — reading a source's health and acting on it are separate
   * privileges, so an Analyst keeps the watch and cannot press this.
   *
   * Returns the job that will actually run: clicking twice while one is queued names
   * the same row rather than starting a second walk.
   */
  resyncSource: (sourceId: string) =>
    call<SourceSyncQueued>(`/v1/sources/${encodeURIComponent(sourceId)}/sync`, {
      method: "POST",
    }),

  /**
   * Cancel an invitation that is still waiting. Requires `member:invite`.
   *
   * A 404 means it is no longer waiting — accepted, already cancelled, or never this
   * organisation's. The page treats all three the same way, because from the reader's
   * side they are the same thing: the row they clicked is stale, so refetch.
   */
  revokeInvitation: (invitationId: string) =>
    call<RevokeInvitationResponse>(
      `/v1/invitations/${encodeURIComponent(invitationId)}/revoke`,
      { method: "POST" },
    ),

  /**
   * Issue a fresh invitation to the same address, killing the old one.
   *
   * Not a re-delivery of the same token: reusing it would extend a live credential's
   * life on every press. The rank ceiling is re-checked against whoever pressed this,
   * not whoever sent the original.
   */
  resendInvitation: (invitationId: string) =>
    call<InviteResponse>(`/v1/invitations/${encodeURIComponent(invitationId)}/resend`, {
      method: "POST",
    }),

  /** Every invitation and what happened to it. Requires `member:invite`. */
  invitations: (params: { cursor?: string | null } = {}) => {
    const search = new URLSearchParams();
    if (params.cursor) search.set("cursor", params.cursor);
    const suffix = search.size ? `?${search}` : "";
    return call<InvitationPage>(`/v1/invitations${suffix}`, { method: "GET" });
  },

  /**
   * Change a member role. Requires `member:assign_role` — and the server refuses
   * self-changes, peers, and any grant at or above the actor rank, whatever the
   * browser believed.
   */
  assignRole: (userId: string, body: RoleChangeBody) =>
    call<RoleChangeResponse>(`/v1/employees/${encodeURIComponent(userId)}/role`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  /**
   * The role taxonomy catalogue. Requires `profile:self_read`, which every role holds —
   * an employee who cannot read it cannot be shown the NAME of their own seniority.
   *
   * Global reference data, identical for every organisation, so it is safe to cache for
   * the life of the page.
   */
  roleCatalogue: () =>
    call<RoleTaxonomyCatalogue>("/v1/role-catalogue", { method: "GET" }),

  /** One employee's assignment. Requires `member:read`. */
  roleAssignment: (userId: string) =>
    call<RoleAssignment>(`/v1/employees/${encodeURIComponent(userId)}/role-assignment`, {
      method: "GET",
    }),

  /**
   * Set an employee's practice, title, normalized level and platform role code.
   * Requires `member:assign_role_code`.
   *
   * Distinct from `assignRole` above, which moves somebody between RBAC roles and
   * changes what they may DO. This changes only where they sit on the org chart. The
   * server additionally refuses a governance seat (CHM/CEO/ITA/HRA) to anyone below
   * Owner or Super Admin, and to oneself.
   */
  assignRoleTaxonomy: (userId: string, body: RoleAssignmentBody) =>
    call<RoleAssignment>(
      `/v1/employees/${encodeURIComponent(userId)}/role-assignment`,
      { method: "PATCH", body: JSON.stringify(body) },
    ),

  /** Rename the organisation. Requires `org:update`. The domain is immutable. */
  renameOrganisation: (body: OrgRenameBody) =>
    call<OrgRenameResponse>("/v1/orgs/current", {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  /** Dashboard counts, each a real aggregate. Requires `org:read`. */
  overview: () => call<OrgOverview>("/v1/orgs/current/overview", { method: "GET" }),

  /**
   * When this organisation's connected providers are re-read (ADR 0018).
   *
   * Requires `integration:self_manage`, which every role holds: an employee who can
   * connect a tool is entitled to know when it will be read again. The run history —
   * `last_started_at` and the four fields beside it — arrives as **nulls** for a caller
   * without `org:read`, so a surface must not read a null there as "it has never run".
   */
  syncSchedule: () => call<SyncSchedule>("/v1/orgs/current/sync-schedule", { method: "GET" }),

  /**
   * Set the schedule. Requires `org:update`. The body carries the whole schedule, not a
   * patch, and nothing else — the endpoint rejects unknown fields outright. A timezone
   * the server does not recognise comes back as a 422 naming the zone, which the caller
   * is expected to show rather than translate.
   */
  updateSyncSchedule: (body: SyncScheduleBody) =>
    call<SyncSchedule>("/v1/orgs/current/sync-schedule", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  /**
   * The readiness probe. Public on the API itself — the platform polls it with no
   * session — and typed loosely because its checks map grows with the deployment.
   */
  ready: () =>
    call<{ status: string; checks: Record<string, string>; request_id: string }>("/readyz", {
      method: "GET",
    }),

  /** The role catalogue as the database seeds it. Requires `org:read`. */
  roles: () => call<RoleCatalogue>("/v1/roles", { method: "GET" }),


  /**
   * The integration catalogue with the caller's own connections merged in.
   * `configured: false` renders as "not configured for this deployment" — the UI
   * never fakes a Connect for a provider the backend cannot serve.
   */
  integrations: () => call<IntegrationCatalogue>("/v1/integrations", { method: "GET" }),

  /**
   * One employee's connections, counts and states only. Requires `integration:read`.
   * Governance reads operational metadata; content and credentials stay out of reach.
   */
  employeeConnections: (userId: string) =>
    call<EmployeeConnections>(`/v1/employees/${encodeURIComponent(userId)}/connections`, {
      method: "GET",
    }),

  /** Administrative revocation, rank-checked server-side. Requires `integration:revoke`. */
  revokeConnection: (connectionId: string) =>
    call<void>(`/v1/connections/${encodeURIComponent(connectionId)}`, { method: "DELETE" }),

  /**
   * Begin the OAuth flow for the CALLING employee. The response carries the provider's
   * authorize URL; the browser NAVIGATES there — it is never fetched.
   */
  connect: (providerId: string) =>
    call<ConnectStarted>(`/v1/me/connections/${encodeURIComponent(providerId)}`, {
      method: "POST",
    }),

  /** Disconnect the caller's own connection. Deletes the stored credential. */
  disconnectIntegration: (connectionId: string) =>
    call<void>(`/v1/me/connections/${encodeURIComponent(connectionId)}`, {
      method: "DELETE",
    }),

  /** Queue a sync of the caller's own connection into the durable job queue. */
  syncNow: (connectionId: string) =>
    call<SyncQueued>(`/v1/me/connections/${encodeURIComponent(connectionId)}/sync`, {
      method: "POST",
    }),

  /** Per-provider aggregate for governance. Counts, never identities. */
  connectionSummary: () => call<ConnectionSummary>("/v1/connections/summary", { method: "GET" }),

  /** The organisation's allow/deny per provider. Absence of a row means allowed. */
  connectionPolicies: () => call<ConnectionPolicies>("/v1/connection-policies", { method: "GET" }),

  /** Allow or restrict one provider org-wide. Does not sever existing connections. */
  setConnectionPolicy: (providerId: string, allowed: boolean) =>
    call<PolicyOut>(`/v1/connection-policies/${encodeURIComponent(providerId)}`, {
      method: "PUT",
      body: JSON.stringify({ allowed }),
    }),


  /** The scope categories the backend can actually serve — the wizard offers no more. */
  ktScopes: () => call<KtScopes>("/v1/kt/scopes", { method: "GET" }),

  /** Create a package. Creates no access; scope narrows presentation only. */
  ktCreate: (body: KtCreateBody) =>
    call<KtAdmin>("/v1/kt", { method: "POST", body: JSON.stringify(body) }),

  ktList: (params: { cursor?: string | null } = {}) => {
    const search = new URLSearchParams();
    if (params.cursor) search.set("cursor", params.cursor);
    const suffix = search.size ? `?${search}` : "";
    return call<KtAdminPage>(`/v1/kt${suffix}`, { method: "GET" });
  },

  /** One package, for the admin detail view. The route existed since 0013; the client
   *  method did not, which is why nothing rendered `last_activity_at`. */
  ktGet: (id: string) => call<KtAdmin>(`/v1/kt/${encodeURIComponent(id)}`, { method: "GET" }),

  ktRevoke: (id: string) =>
    call<KtAdmin>(`/v1/kt/${encodeURIComponent(id)}/revoke`, { method: "POST" }),

  ktComplete: (id: string) =>
    call<KtAdmin>(`/v1/kt/${encodeURIComponent(id)}/complete`, { method: "POST" }),

  /** Extend the expiry (from the later of now and the current expiry, never past a year
   *  out) or re-address a package nobody has opened yet. A revoked or completed package
   *  answers 409, as does re-addressing one that is already bound to its recipient. */
  ktUpdate: (id: string, body: KtUpdateBody) =>
    call<KtAdmin>(`/v1/kt/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  /**
   * Open a package addressed to you, claiming it on first open. Every refusal is the
   * server's: revoked and expired arrive as 403s carrying the exact sentence to show,
   * everything else as an indistinguishable 404.
   */
  ktClaim: (ktCode: string) =>
    call<KtRecipient>("/v1/kt/claim", {
      method: "POST",
      body: JSON.stringify({ kt_code: ktCode }),
    }),

  /** Documents in the package window the RECIPIENT may already read (their own ACL). */
  /** One document in the package window the recipient may read, as ordered masked
   *  chunks. A document outside the window, or one their own ACL does not admit, is the
   *  same 404 as one that does not exist — the server never distinguishes them. */
  ktDocument: (ktCode: string, documentId: string, params: { fromOrdinal?: number } = {}) => {
    const search = new URLSearchParams();
    if (params.fromOrdinal) search.set("from_ordinal", String(params.fromOrdinal));
    const suffix = search.size ? `?${search}` : "";
    return call<KtDocumentDetail>(
      `/v1/kt/${encodeURIComponent(ktCode)}/documents/${encodeURIComponent(documentId)}${suffix}`,
      { method: "GET" },
    );
  },

  ktDocuments: (ktCode: string, params: { cursor?: string | null } = {}) => {
    const search = new URLSearchParams();
    if (params.cursor) search.set("cursor", params.cursor);
    const suffix = search.size ? `?${search}` : "";
    return call<KtDocumentPage>(
      `/v1/kt/${encodeURIComponent(ktCode)}/documents${suffix}`,
      { method: "GET" },
    );
  },


  /**
   * A grounded answer over retrieved evidence, or an honest refusal.
   *
   * POST for the same reason search is: the question is user-authored text that must
   * not reach access logs in a URL. The body carries only the question and k — the
   * model, the prompt and the grounding gate are all server-side (§28), so nothing a
   * browser sends can influence which model answers or how it is checked.
   */
  ask: (body: { question: string; k?: number }) =>
    call<AskResponse>("/v1/ask", { method: "POST", body: JSON.stringify(body) }),


  /** The caller's authorized knowledge context: real ACL-filtered counts, never content. */
  myKnowledge: () => call<MyKnowledge>("/v1/me/knowledge", { method: "GET" }),

  /** Departments as people declared them on their own profiles, with member counts. */
  departments: () => call<Departments>("/v1/departments", { method: "GET" }),


  /**
   * Extracted, quote-gated claims the recipient may read. `type` narrows to one
   * claim type; omitted, every in-scope type arrives date-ordered — the timeline.
   */
  ktInsights: (
    ktCode: string,
    params: { type?: string | null; limit?: number } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.type) search.set("type", params.type);
    if (params.limit) search.set("limit", String(params.limit));
    const suffix = search.size ? `?${search}` : "";
    return call<KtInsights>(
      `/v1/kt/${encodeURIComponent(ktCode)}/insights${suffix}`,
      { method: "GET" },
    );
  },

  /** Counts per claim type, under the same ACL predicate that serves the rows. */
  ktInsightSummary: (ktCode: string) =>
    call<KtInsightSummary>(
      `/v1/kt/${encodeURIComponent(ktCode)}/insights-summary`,
      { method: "GET" },
    ),

  /**
   * §29's executive summary, composed on demand from claims the recipient may read,
   * citation-gated server-side exactly like /v1/ask. Never cached beyond the query —
   * a stored summary would outlive the ACL state it was grounded in.
   */
  ktHandoverSummary: (ktCode: string) =>
    call<KtHandoverSummary>(
      `/v1/kt/${encodeURIComponent(ktCode)}/handover-summary`,
      { method: "GET" },
    ),

  // ---------------------------------------------------------------- KT console
  //
  // Everything below is the recipient's own: the conversation, what they saved, how
  // far they are. Every route re-runs the package's authorization server-side, so a
  // revoked package answers 403 to all of these on the next request — keep `staleTime`
  // short and never cache across packages.

  /**
   * One turn of the KT copilot. Same retrieval and the same grounding gate as Ask
   * JUTSU, narrowed to the package window, with the conversation so far as context.
   * POST because the question is user-authored text; nothing here names a model.
   */
  ktAsk: (ktCode: string, body: KtCopilotAskBody) =>
    call<KtCopilotTurn>(`/v1/kt/${encodeURIComponent(ktCode)}/ask`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  ktConversations: (ktCode: string, params: { cursor?: string | null } = {}) => {
    const search = new URLSearchParams();
    if (params.cursor) search.set("cursor", params.cursor);
    const suffix = search.size ? `?${search}` : "";
    return call<KtConversationPage>(
      `/v1/kt/${encodeURIComponent(ktCode)}/conversations${suffix}`,
      { method: "GET" },
    );
  },

  /** Search earlier conversations by what was said. POST: the words stay out of the URL. */
  ktSearchConversations: (ktCode: string, q: string) =>
    call<KtConversationPage>(
      `/v1/kt/${encodeURIComponent(ktCode)}/conversations/search`,
      { method: "POST", body: JSON.stringify({ q }) },
    ),

  ktConversation: (ktCode: string, conversationId: string) =>
    call<KtConversationDetail>(
      `/v1/kt/${encodeURIComponent(ktCode)}/conversations/${encodeURIComponent(conversationId)}`,
      { method: "GET" },
    ),

  ktArchiveConversation: (ktCode: string, conversationId: string) =>
    call<void>(
      `/v1/kt/${encodeURIComponent(ktCode)}/conversations/${encodeURIComponent(conversationId)}/archive`,
      { method: "POST" },
    ),

  ktBookmarks: (ktCode: string) =>
    call<KtBookmarks>(`/v1/kt/${encodeURIComponent(ktCode)}/bookmarks`, { method: "GET" }),

  /** Save a claim, document, message or free-text question. A second save of the same
   *  referent updates its note rather than duplicating it. */
  ktBookmark: (ktCode: string, body: KtBookmarkBody) =>
    call<KtBookmark>(`/v1/kt/${encodeURIComponent(ktCode)}/bookmarks`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  ktRemoveBookmark: (ktCode: string, bookmarkId: string) =>
    call<void>(
      `/v1/kt/${encodeURIComponent(ktCode)}/bookmarks/${encodeURIComponent(bookmarkId)}`,
      { method: "DELETE" },
    ),

  ktProgress: (ktCode: string) =>
    call<KtProgressList>(`/v1/kt/${encodeURIComponent(ktCode)}/progress`, { method: "GET" }),

  /** `seen | done | unclear` against `claim:{id}` | `document:{id}` | `step:{key}`. */
  ktSetProgress: (ktCode: string, itemKey: string, state: KtProgressState) =>
    call<KtProgressItem>(
      `/v1/kt/${encodeURIComponent(ktCode)}/progress/${encodeURIComponent(itemKey)}`,
      { method: "PUT", body: JSON.stringify({ state }) },
    ),

  ktClearProgress: (ktCode: string, itemKey: string) =>
    call<void>(
      `/v1/kt/${encodeURIComponent(ktCode)}/progress/${encodeURIComponent(itemKey)}`,
      { method: "DELETE" },
    ),

  /** Coverage, learning path, recommendations, gaps and the resume card — one call,
   *  all computed now from what this recipient may read. */
  ktWorkspace: (ktCode: string) =>
    call<KtWorkspace>(`/v1/kt/${encodeURIComponent(ktCode)}/workspace`, { method: "GET" }),

  logout: () => call<void>("/v1/auth/logout", { method: "POST" }),
};
