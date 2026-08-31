import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { MemberShell } from "@/components/member/member-shell";
import { hasSessionCookie } from "@/lib/auth";
import { SIGN_IN_PATH } from "@/lib/surfaces";

export const metadata: Metadata = {
  // Behind a session, and never worth indexing.
  robots: { index: false, follow: false },
};

/**
 * The surface for someone who is *in* an organisation rather than running one.
 *
 * It exists because `/me` did not, and both the invitation-acceptance flow and (once
 * sign-in stopped sending everybody to `/admin`) the sign-in flow named it as their
 * destination. A Member finished onboarding — correct code, real JUTSU ID, valid
 * session — and landed on a 404. The API had been right the whole time; there was
 * simply nowhere for it to point.
 *
 * Deliberately not `AdminShell`. That shell renders a sidebar filtered by permission,
 * and a Member holds none of them, so reusing it would produce a dashboard framed by an
 * empty navigation column — which reads as broken rather than as "this is your page".
 *
 * The cookie check is a *navigation* convenience and not a security control, exactly as
 * in the admin layout: the cookie is opaque, validating it is the API's job, and a
 * forged one buys a shell whose every request 401s.
 */
export default async function MeLayout({ children }: { children: React.ReactNode }) {
  if (!(await hasSessionCookie())) {
    redirect(SIGN_IN_PATH);
  }

  return <MemberShell>{children}</MemberShell>;
}
