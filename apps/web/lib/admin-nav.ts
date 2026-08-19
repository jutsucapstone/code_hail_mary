import type { Permission } from "@/lib/permissions";

/**
 * The admin dashboard's information architecture.
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
 * section that has no endpoint yet says so rather than showing invented figures.
 */

export type SectionStatus = "live" | "pending";

export interface AdminSection {
  slug: string;
  name: string;
  description: string;
  permission: Permission;
  status: SectionStatus;
  /** The slice that makes it live. Shown to the reader, not hidden in a comment. */
  slice: string;
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
  {
    slug: "employees",
    name: "Employees",
    description: "Invite people, issue JUTSU IDs, and manage access.",
    permission: "member:read",
    status: "live",
    slice: "P1",
  },
  {
    slug: "integrations",
    name: "Integrations",
    description: "Connect the tools your organisation already runs on.",
    permission: "integration:read",
    status: "pending",
    slice: "P3",
  },
  {
    slug: "roles",
    name: "Roles & permissions",
    description: "Who can do what, and why.",
    permission: "member:assign_role",
    status: "pending",
    slice: "P2",
  },
  {
    slug: "settings",
    name: "Organisation",
    description: "Name, domain and organisation-wide settings.",
    permission: "org:update",
    status: "pending",
    slice: "P2",
  },
  {
    slug: "audit",
    name: "Audit log",
    description: "Every security-sensitive action, immutably recorded.",
    permission: "audit:read",
    status: "pending",
    slice: "P2",
  },
] as const;

export const adminHref = (slug: string): string => (slug ? `/admin/${slug}` : "/admin");
