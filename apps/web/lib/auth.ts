/**
 * Stub authentication.
 *
 * Real auth is Google Identity Platform with OAuth2/SAML and httpOnly rotating
 * sessions (§17). None of that exists yet, so this is a single cookie whose *presence*
 * gates the product routes.
 *
 * It is deliberately trivial and deliberately obvious. The one thing it gets right is
 * the shape of the boundary — `getSession()` is the only way any surface learns who the
 * caller is, so swapping the implementation later touches this file and nothing else.
 *
 * This is not a security control. It gates navigation, not data. Every real access
 * decision happens server-side against source ACLs (§4.5, §4.6), which is why no
 * product route may read a permission from this session.
 */

import { cookies } from "next/headers";

export const SESSION_COOKIE = "jutsu_session";

export interface Session {
  userId: string;
  orgId: string;
}

/**
 * The signed-in caller, or null.
 *
 * The cookie carries an opaque id only — never an email or display name — so a stray
 * log line cannot leak PII (§4.9).
 */
export async function getSession(): Promise<Session | null> {
  const store = await cookies();
  const raw = store.get(SESSION_COOKIE)?.value;
  if (!raw) return null;

  // Stub format: "<userId>:<orgId>". Replaced wholesale by a verified JWT at S29.
  const [userId, orgId] = raw.split(":");
  if (!userId || !orgId) return null;

  return { userId, orgId };
}

export async function requireSession(): Promise<Session> {
  const session = await getSession();
  if (!session) {
    // Middleware redirects before a product page renders, so reaching here means the
    // matcher and the route tree have drifted apart.
    throw new Error("requireSession called without a session — check middleware matcher");
  }
  return session;
}
