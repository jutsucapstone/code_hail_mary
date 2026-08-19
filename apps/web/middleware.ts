import { NextResponse, type NextRequest } from "next/server";

import { SESSION_COOKIE } from "@/lib/auth";
import { PILOT_PATH, PRODUCT_PATHS } from "@/lib/surfaces";

/** Prefixes that need a session cookie before they are worth rendering. */
const GATED_PREFIXES = [...PRODUCT_PATHS, "/admin"];

/**
 * Unauthenticated traffic sees the marketing site; authenticated traffic can reach the
 * product surfaces (§16).
 *
 * Only cookie *presence* is checked here. Middleware runs on the edge before any data
 * is touched, so it is a navigation gate, never an authorisation one — every real
 * access decision is made server-side against source ACLs inside the query (§4.5).
 */
export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // The API proxy is intentionally not gated here. It forwards to FastAPI, which makes
  // every authorization decision itself — gating it in middleware would put an access
  // decision in Next, which is exactly what this architecture refuses. Stated explicitly
  // because the matcher below does match /api/*, so silence would read as an oversight.
  if (pathname.startsWith("/api/jutsu/")) return NextResponse.next();

  const isGated = GATED_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
  if (!isGated) return NextResponse.next();

  if (request.cookies.has(SESSION_COOKIE)) return NextResponse.next();

  // Send them home with the intended destination preserved, so the eventual sign-in
  // flow can return them to it rather than dumping them on the landing page.
  const url = request.nextUrl.clone();
  url.pathname = PILOT_PATH;
  // Deliberately no `next` parameter. The destination after sign-in is chosen by the
  // API and returned in its response — honouring one from the URL here would be an open
  // redirect with a freshly minted session attached.
  url.search = "";
  return NextResponse.redirect(url);
}

export const config = {
  /**
   * Static assets and metadata routes are excluded — they are public by definition and
   * matching them would run this on every image request.
   */
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|icon.png|apple-icon.png|opengraph-image|robots.txt|sitemap.xml|manifest.webmanifest).*)",
  ],
};
