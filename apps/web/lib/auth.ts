import { cookies } from "next/headers";

/**
 * The session cookie, as far as the frontend is concerned.
 *
 * This module used to decode a `"<userId>:<orgId>"` stub and hand back a `Session`. It
 * cannot any more, and that is the point: the cookie is now 256 bits of opaque random
 * data with no claims in it at all. There is no organisation id to read, no user id, no
 * role — nothing for a route here to branch on even if someone wanted to.
 *
 * So the only question this file can answer is "is there a cookie". That is a
 * *navigation* question, and answering it just avoids rendering a shell that would
 * immediately 401. Every real access decision — who the caller is, which tenant they
 * belong to, and what they may do — is made by FastAPI against the database, on every
 * request, including the ones this file's answer let through.
 *
 * If a future change needs the org id in the browser, it comes from `GET /v1/me`, which
 * is the API stating a fact about the caller. It never comes from the cookie, and it is
 * never accepted back as an authorisation input.
 */

/** Name only. The value is opaque and the browser never needs to look inside it. */
export const SESSION_COOKIE = "__Host-jutsu_session";

/**
 * Whether a session cookie is present. Not whether it is valid.
 *
 * Deliberately boolean. A function that returned the token would invite a caller to do
 * something with it, and there is nothing correct to do with it here.
 */
export async function hasSessionCookie(): Promise<boolean> {
  const store = await cookies();
  return store.has(SESSION_COOKIE);
}
