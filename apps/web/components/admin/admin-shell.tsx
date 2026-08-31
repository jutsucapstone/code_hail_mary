"use client";

import { useMemo } from "react";

import { ConsoleShell, type ShellSection } from "@/components/console/console-shell";
import { ADMIN_SECTIONS, adminHref } from "@/lib/admin-nav";

/**
 * The management console's chrome.
 *
 * Everything structural — the header, the `GET /v1/me` fetch, the capabilities context,
 * sign-out, the redirect on an expired session, the loading state — lives in
 * `ConsoleShell`, which the employee console shares. This file is now only the two facts
 * that are actually specific to administration: *these* sections, and the sidebar layout
 * that a permission-filtered section list needs.
 *
 * Visually it is the same design system as the marketing site — same tokens, same
 * hairlines, same micro-label — with a sidebar instead of a centred column, because §16
 * is explicit that a product looking unrelated to its own landing page reads as
 * unfinished.
 */

/**
 * The signed-in administrator.
 *
 * Re-exported under its established name so the pages that import it did not have to
 * change. It is the same context the employee console uses; there was never a reason for
 * two.
 */
export { useConsoleCapabilities as useCapabilities } from "@/components/console/console-shell";

export function AdminShell({ children }: { children: React.ReactNode }) {
  const sections = useMemo<ShellSection[]>(
    () =>
      ADMIN_SECTIONS.map((section) => ({
        href: adminHref(section.slug),
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
    <ConsoleShell sections={sections} navLabel="Admin sections" variant="sidebar">
      {children}
    </ConsoleShell>
  );
}
