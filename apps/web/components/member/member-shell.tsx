"use client";

import { useMemo } from "react";

import { ConsoleShell, type ShellSection } from "@/components/console/console-shell";
import { MEMBER_SECTIONS } from "@/lib/member-nav";

/**
 * Chrome for a person who belongs to an organisation without administering it.
 *
 * Structure is shared with the management console through `ConsoleShell` — same header,
 * same identity fetch, same sign-out, same expired-session handling. What stays different
 * is the `inline` variant: a horizontal section strip over a prose-width column rather
 * than a sidebar.
 *
 * That difference is deliberate and not cosmetic. A sidebar is filtered by permission,
 * and a bare Member holds almost none — `profile:self_read` and `retrieval:query` are in
 * every role's set, while `org:read` and `member:read` are not. Rendering their console
 * framed by a near-empty navigation column reads as broken rather than as "this is your
 * page".
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
      })),
    [],
  );

  return (
    <ConsoleShell sections={sections} navLabel="Your console" variant="inline">
      {children}
    </ConsoleShell>
  );
}
