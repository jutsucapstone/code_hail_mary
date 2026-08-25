import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { AdminShell } from "@/components/admin/admin-shell";
import { hasSessionCookie } from "@/lib/auth";
import { SIGN_IN_PATH } from "@/lib/surfaces";
import { MAIN_CONTENT_ID } from "@/lib/landmarks";

export const metadata: Metadata = {
  // Behind a session, and never worth indexing.
  robots: { index: false, follow: false },
};

/**
 * The admin dashboard shell.
 *
 * The cookie check here is a *navigation* convenience, not a security control. It only
 * avoids rendering chrome that would immediately fail its first request — it cannot tell
 * whether the session is valid, because the cookie is opaque and validating it is the
 * API's job. Someone who forges a cookie gets a shell whose every request 401s, which is
 * the correct outcome.
 *
 * That distinction is the whole architecture: Next owns the cookie as transport and makes
 * no access decision. If this file ever grows a check on *what* the caller may do, that
 * is the bug.
 */
export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  if (!(await hasSessionCookie())) {
    redirect(SIGN_IN_PATH);
  }

  return (
    <div id={MAIN_CONTENT_ID}>
      <AdminShell>{children}</AdminShell>
    </div>
  );
}
