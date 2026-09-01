import type { Permission } from "@/lib/permissions";

/**
 * The employee console's information architecture (§6).
 *
 * The member shell had no navigation at all — a header and a page. That was honest when
 * `/me` was the only destination a Member could reach; it stopped being honest once
 * `/ask` and the identity endpoints existed.
 *
 * `status` is what keeps this list from lying. My Integrations went live when the
 * connection lifecycle landed (migration 0012 + /v1/integrations); Knowledge Transfer
 * still needs a KT model that does not exist yet, so it stays listed as text with the
 * slice that delivers it rather than as a link onto a 404 (§4.11).
 *
 * `permission` gates *rendering* only. Every endpoint behind these re-checks server-side,
 * so a caller who types a hidden URL gets a 403 from the API and not a page.
 */

export type MemberSectionStatus = "live" | "pending";

export interface MemberSection {
  /** Absolute path. Empty string means the console root. */
  href: string;
  name: string;
  description: string;
  /** `null` where the section needs nothing beyond an authenticated session. */
  permission: Permission | null;
  status: MemberSectionStatus;
  /** The slice that makes it live. Shown to the reader, not buried in a comment. */
  slice: string;
  /** Sidebar IA group (§6). Ungrouped sections render as the flat run at the top. */
  group?: string;
}

export const MEMBER_SECTIONS: readonly MemberSection[] = [
  {
    href: "/me",
    name: "Home",
    description: "Your JUTSU identity and what it gives you access to.",
    permission: "profile:self_read",
    status: "live",
    slice: "P1",
  },
  {
    href: "/ask",
    name: "Ask JUTSU",
    group: "Knowledge",
    description: "Search the organisational memory you are authorised to read.",
    permission: "retrieval:query",
    status: "live",
    slice: "P1",
  },
  {
    href: "/me/knowledge",
    name: "My knowledge",
    group: "Knowledge",
    description: "What your authorised context contains, counted honestly.",
    permission: "retrieval:query",
    status: "live",
    slice: "P3",
  },
  {
    href: "/handover",
    name: "Knowledge transfer",
    group: "Knowledge",
    description: "Open a knowledge-transfer package shared with you.",
    permission: null,
    status: "live",
    slice: "S26–S27",
  },
  {
    href: "/me/integrations",
    name: "My integrations",
    group: "Integrations",
    description: "Connect your own applications so their content can be indexed.",
    permission: null,
    status: "live",
    slice: "P3",
  },
  {
    // Read-only here. `GET /v1/me/identities` exists; nothing writes a member's own
    // links, and nothing should — linking a subject to yourself is precisely the
    // escalation the API refuses for administrators.
    href: "/me/identities",
    name: "Source identities",
    // Under Access, never Integrations — an identity GRANTS document visibility, a
    // connector merely fetches content, and filing them together is what invites a
    // "disconnect" that silently revokes access (the console-traps rule).
    group: "Access",
    description: "The accounts you are known by, and the documents they let you read.",
    permission: "integration:self_manage",
    status: "live",
    slice: "P1",
  },
  {
    // Live as of Phase 3A: GET/PATCH /v1/me/profile read and write the
    // `employee_profiles` row that has existed since migration 0002. Both permissions
    // are in `_EVERYONE`, so gating on `profile:self_read` hides this from nobody — it
    // is stated because the endpoint requires it, not to filter anyone out.
    href: "/me/profile",
    name: "Profile",
    group: "Account",
    description: "Your role, team and working context.",
    permission: "profile:self_read",
    status: "live",
    slice: "P1",
  },
] as const;
