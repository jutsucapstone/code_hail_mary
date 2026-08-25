import type { components } from "@/lib/api-schema";

/**
 * Permission strings, derived from the generated API schema rather than retyped.
 *
 * §4.13 forbids hand-written frontend types for API data, and a permission literal is
 * exactly that: if the catalogue changes and this list does not, the UI silently hides a
 * section the caller can in fact use, or shows one they cannot. Deriving it means a
 * regenerated schema breaks the build instead.
 *
 * These decide what to *render*. They never decide what is *allowed* — every endpoint
 * behind them re-checks server-side, and a caller who types the URL gets a 403 from the
 * API regardless of what the browser believed.
 */
export type Capabilities = components["schemas"]["Capabilities"];

/** One permission string, as the API spells it. */
export type Permission = Capabilities["permissions"][number];

export function can(capabilities: Capabilities | null, permission: Permission): boolean {
  return capabilities?.permissions.includes(permission) ?? false;
}

/** One role, as the API spells it. */
export type Role = Capabilities["role"];

/**
 * Human labels for roles.
 *
 * Typed as a total record over `Role` rather than `Record<string, string>`, so adding a
 * role to the API and regenerating the schema fails the build here instead of rendering
 * the raw enum value — `hr_admin` in the middle of a sentence is the kind of thing that
 * ships because nobody owned the mapping.
 *
 * Shared rather than re-declared: this was private to the employees table, and the
 * member page needs the same words for the same values.
 */
export const ROLE_LABELS: Record<Role, string> = {
  owner: "Organisation owner",
  super_admin: "Super admin",
  hr_admin: "HR admin",
  it_admin: "IT admin",
  analyst: "Analyst",
  viewer: "Viewer",
  member: "Member",
};
