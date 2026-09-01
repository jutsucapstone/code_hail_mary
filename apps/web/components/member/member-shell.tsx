"use client";

import { useMemo } from "react";

import { ConsoleShell, type ShellSection } from "@/components/console/console-shell";
import { MEMBER_SECTIONS } from "@/lib/member-nav";

/**
 * Chrome for a person who belongs to an organisation without administering it.
 *
 * Structure is shared with the management console through `ConsoleShell` — same header,
 * same identity fetch, same sign-out, same expired-session handling, same grouped
 * sidebar. This surface once used the `inline` strip because a permission-filtered
 * sidebar would have been nearly empty for a bare Member; that stopped being true the
 * release every section here became visible to every role (each is either
 * permission-null or gated on a permission in `_EVERYONE`), and at seven sections the
 * strip wrapped to two lines — which reads as a layout accident, not a navigation.
 *
 * The same constraint is why this surface shows a JUTSU ID and a role rather than an
 * organisation name: the name lives behind `GET /v1/orgs/current`, which requires
 * `org:read`, and asking for it would earn a 403 for exactly the people this page exists
 * for.
 */

/**
 * The signed-in member.
 *
 * Re-exported under its established name so `/me` did not have to change. It is the same
 * context the management console uses.
 */
export { useConsoleCapabilities as useMemberCapabilities } from "@/components/console/console-shell";

export function MemberShell({ children }: { children: React.ReactNode }) {
  const sections = useMemo<ShellSection[]>(
    () =>
      MEMBER_SECTIONS.map((section) => ({
        href: section.href,
        name: section.name,
        description: section.description,
        status: section.status,
        slice: section.slice,
        permission: section.permission,
        group: section.group,
      })),
    [],
  );

  return (
    <ConsoleShell sections={sections} navLabel="Your console" variant="sidebar">
      {children}
    </ConsoleShell>
  );
}
