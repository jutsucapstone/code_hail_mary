import type { Permission } from "@/lib/permissions";

/**
 * The management console's information architecture.
 *
 * One list drives the sidebar, the mobile nav and the section headings, so a section
 * cannot be linked without also being routed.
 *
 * `permission` is what the *server* requires for that section's data. Rendering the link
 * is gated on the same value, which is a courtesy — hiding a door the caller cannot open
 * — and never the enforcement. Every one of these endpoints re-checks server-side, and a
 * caller who types the URL gets a 403 from the API rather than a rendered page.
 *
 * `status` is honest about what exists. §4.11 forbids mock data behind a UI surface, so a
 * section that has no endpoint yet says so rather than showing invented figures. Pending
 * sections render as text with their slice, never as links — showing the shape of the
 * product without promising a door that opens.
 *
 * ---
 *
 * **Two placements in here are decisions, not filing.**
 *
 * *Source identities sits under Access, not Integrations.* Linking a source identity
 * writes the namespaced provider subject that `document_acl` grants are matched against,
 * so it **grants document access**; an integration fetches content. They are different
 * operations with different consequences. Filing this screen under an "Integrations"
 * heading is precisely what invites somebody to wire a Disconnect button to
 * `DELETE .../identities/{id}` and silently revoke a colleague's access to documents. The
 * heading a screen sits under is a label like any other, and this one has to be right.
 *
 * *Several pending sections gate on the nearest existing permission, not their own.*
 * There is no `kt:read`, no `job:read` and no `source:read` — the catalogue has fifteen
 * permissions and none of them covers knowledge transfer, ingestion jobs or knowledge
 * sources. Those sections gate on `org:read` or `integration:read` so that the shape of
 * the product is visible to the people who will administer it, and each is marked below.
 * When the domain lands it brings its own permission, and these must move to it — a
 * permission is part of an endpoint's contract, and borrowing one is only tolerable while
 * there is no endpoint to contradict.
 */

export type SectionStatus = "live" | "pending";

/** The IA groups, in render order. `null` is the ungrouped run at the top. */
export type AdminGroup = "People" | "Knowledge" | "Integrations" | "Access" | "Operations";

export interface AdminSection {
  slug: string;
  name: string;
  description: string;
  permission: Permission;
  status: SectionStatus;
  /** The slice that makes it live. Shown to the reader, not hidden in a comment. */
  slice: string;
  /** Omitted for the top-level entry, which needs no heading above it. */
  group?: AdminGroup;
}

export const ADMIN_SECTIONS: readonly AdminSection[] = [
  {
    slug: "",
    name: "Overview",
    description: "Your organisation at a glance.",
    permission: "org:read",
    status: "live",
    slice: "P1",
  },

  // ---- People -------------------------------------------------------------------
  {
    slug: "employees",
    name: "Employees",
    description: "Invite people, issue JUTSU IDs, and manage access.",
    permission: "member:read",
    status: "live",
    slice: "P1",
    group: "People",
  },
  {
    // Sending stays on the Employees page, next to the people list; this section is the
    // ledger of what happened to each one, over `GET /v1/invitations`.
    slug: "invitations",
    name: "Invitations",
    description: "Outstanding invitations, and what happened to them.",
    permission: "member:invite",
    status: "live",
    slice: "P2",
    group: "People",
  },
  {
    // Department is a nullable free-text `varchar(128)` on `employee_profiles` that each
    // person types themselves. It is not an entity: nothing lists departments, nothing
    // validates one, and two spellings are two departments. This section needs that to
    // become a real table first.
    slug: "departments",
    name: "Departments",
    description: "Group people by team, and see knowledge by department.",
    permission: "member:read",
    status: "pending",
    slice: "P2",
    group: "People",
  },

  // ---- Knowledge ----------------------------------------------------------------
  {
    // Borrowed permission: there is no `kt:*` in the catalogue, because there is no
    // knowledge-transfer domain — no table, no endpoint, no model. Moves to its own
    // permission when that lands.
    slug: "knowledge-transfer",
    name: "Knowledge transfer",
    description: "Create and manage controlled knowledge-transfer packages.",
    permission: "org:read",
    status: "pending",
    slice: "S26–S27",
    group: "Knowledge",
  },
  {
    // `GET /v1/sources` serves sync state and document counts. Creating a source still
    // has no HTTP surface — rows come from ingestion — so this reads and never writes.
    slug: "sources",
    name: "Knowledge sources",
    description: "What has been ingested, from where, and what was excluded.",
    permission: "integration:read",
    status: "live",
    slice: "P3",
    group: "Knowledge",
  },

  // ---- Integrations -------------------------------------------------------------
  {
    slug: "apps",
    name: "Organisation apps",
    description: "Which applications people have connected, and whether they are healthy.",
    permission: "integration:read",
    status: "pending",
    slice: "P3",
    group: "Integrations",
  },
  {
    slug: "connection-policies",
    name: "Connection policies",
    description: "Which applications people may connect, and on what terms.",
    permission: "org:update",
    status: "pending",
    slice: "P3",
    group: "Integrations",
  },

  // ---- Access -------------------------------------------------------------------
  {
    // Under Access, deliberately. See the header comment: this grants document visibility.
    slug: "identities",
    name: "Source identities",
    description: "Which provider accounts each person is known by, and what that grants.",
    permission: "integration:read",
    status: "live",
    slice: "P1",
    group: "Access",
  },
  {
    slug: "roles",
    name: "Roles & permissions",
    description: "Who can do what, and why.",
    permission: "member:assign_role",
    status: "live",
    slice: "P2",
    group: "Access",
  },
  {
    slug: "settings",
    name: "Organisation",
    description: "Name, domain and organisation-wide settings.",
    permission: "org:update",
    status: "live",
    slice: "P2",
    group: "Access",
  },

  // ---- Operations ---------------------------------------------------------------
  {
    // `GET /v1/jobs` + `/v1/jobs/stats`, over the same durable queue the worker runs.
    slug: "jobs",
    name: "Jobs & sync",
    description: "Ingestion and embedding runs, their state, and what failed.",
    permission: "org:read",
    status: "live",
    slice: "P3",
    group: "Operations",
  },
  {
    // `GET /v1/audit` — the first route to declare `audit:read`. The write side was
    // always immutable; now it can be read back by the roles that hold the permission.
    slug: "audit",
    name: "Audit log",
    description: "Every security-sensitive action, immutably recorded.",
    permission: "audit:read",
    status: "live",
    slice: "P2",
    group: "Operations",
  },
  {
    // `/readyz` now probes Postgres for real, so this panel reports something measured.
    slug: "health",
    name: "System health",
    description: "API, queue and connector health, and recent failures.",
    permission: "org:read",
    status: "live",
    slice: "P3",
    group: "Operations",
  },
] as const;

export const adminHref = (slug: string): string => (slug ? `/admin/${slug}` : "/admin");
