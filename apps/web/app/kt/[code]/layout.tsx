import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { KtShell } from "@/components/kt/kt-shell";
import { hasSessionCookie } from "@/lib/auth";
import { SIGN_IN_PATH } from "@/lib/surfaces";

export const metadata: Metadata = {
  robots: { index: false, follow: false },
  title: "Knowledge Transfer",
};

/**
 * The KT console — a distinct product surface, not another dashboard.
 *
 * The cookie check is navigation, never authorization, exactly as in the other
 * layouts: whether THIS person may open THIS package is decided by `POST /v1/kt/claim`
 * inside the shell, on every load. A revoked package renders the server's own sentence
 * however it was reached and whatever was cached (§39).
 */
export default async function KtLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ code: string }>;
}) {
  if (!(await hasSessionCookie())) {
    redirect(SIGN_IN_PATH);
  }
  const { code } = await params;

  return <KtShell code={code}>{children}</KtShell>;
}
