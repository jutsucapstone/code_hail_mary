import { NextResponse, type NextRequest } from "next/server";

import { SESSION_COOKIE } from "@/lib/auth";
import { DEFAULT_SURFACE, PRODUCT_PATHS } from "@/lib/surfaces";

/**
 * The door from the marketing site into the product.
 *
 * `middleware.ts` gates every product route on the presence of the session cookie, and
 * until this route existed nothing anywhere set it — so the six surfaces were routed,
 * rendered and completely unreachable. This is what "Request a pilot" opens.
 *
 * A Route Handler rather than a page because a server component cannot set a cookie
 * during render. Every link to it must therefore be a plain anchor: a `<Link>` would be
 * fetched as RSC by the client router and the cookie would never arrive.
 *
 * This is a preview session, not authentication. It grants navigation, never data —
 * real auth is Google Identity Platform at S29 (§17), and every access decision is made
 * server-side against source ACLs regardless of who holds this cookie (§4.5, §4.6).
 */

/** Eight hours: long enough to browse, short enough that a shared machine forgets. */
const SESSION_MAX_AGE_SECONDS = 60 * 60 * 8;

export async function GET(request: NextRequest) {
  // `next` is attacker-controlled. middleware.ts writes it when it bounces an
  // unauthenticated visitor, but anyone can hand-craft it, so it is matched against the
  // known product paths rather than trusted — an unchecked value here is an open
  // redirect wearing a helpful name.
  const requested = request.nextUrl.searchParams.get("next");
  const destination =
    requested && PRODUCT_PATHS.includes(requested) ? requested : DEFAULT_SURFACE;

  const response = NextResponse.redirect(new URL(destination, request.nextUrl.origin));

  // Stub format from lib/auth.ts: "<userId>:<orgId>". The id is opaque and random, and
  // the org reads `preview` rather than a fabricated tenant, so nothing here is PII and
  // nothing claims to be a real organisation (§4.9, §4.11).
  response.cookies.set(SESSION_COOKIE, `preview-${crypto.randomUUID()}:preview`, {
    // lib/auth.ts reads this server-side only, so no client script needs to see it.
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: SESSION_MAX_AGE_SECONDS,
  });

  return response;
}
