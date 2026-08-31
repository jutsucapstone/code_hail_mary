import type { Permission } from "@/lib/permissions";

/**
 * The employee console's information architecture (§6).
 *
 * The member shell had no navigation at all — a header and a page. That was honest when
 * `/me` was the only destination a Member could reach; it stopped being honest once
 * `/ask` and the identity endpoints existed.
 *
 * `status` is what keeps this list from lying. Four of §6's six destinations have **no
 * backend**: Profile needs an `employee_profiles` endpoint that no router references, My
 * Integrations needs a connector API that does not exist anywhere in the repository, and
 * Knowledge Transfer needs a KT model that has never been built. They are listed, because
 * showing the shape of the product is the point, and they are listed as text with the
 * slice that delivers them rather than as links onto 404s (§4.11).
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
    description: "Search the organisational memory you are authorised to read.",
    permission: "retrieval:query",
    status: "live",
    slice: "P1",
  },
  {
    // Read-only here. `GET /v1/me/identities` exists; nothing writes a member's own
    // links, and nothing should — linking a subject to yourself is precisely the
    // escalation the API refuses for administrators.
    href: "/me/identities",
    name: "Source identities",
    description: "The accounts you are known by, and the documents they let you read.",
    permission: "integration:self_manage",
    status: "live",
    slice: "P1",
  },
  {
    href: "/me/profile",
    name: "Profile",
    description: "Your role, team and working context.",
    permission: null,
    status: "pending",
    slice: "P2",
  },
  {
    href: "/me/integrations",
    name: "My integrations",
    description: "Connect your own applications so their content can be indexed.",
    permission: null,
    status: "pending",
    slice: "P3",
  },
  {
    href: "/handover",
    name: "Knowledge transfer",
    description: "Open a knowledge-transfer package shared with you.",
    permission: null,
    status: "pending",
    slice: "S26–S27",
  },
] as const;
