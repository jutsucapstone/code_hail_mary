import { NextResponse, type NextRequest } from "next/server";

/**
 * The single door from the browser to the API.
 *
 * Every call the frontend makes goes through here, and this handler is deliberately
 * stupid: it forwards a request and returns a response. It does not read the session, it
 * does not inspect a status code, and it makes no decision about what the caller may do.
 * That is the whole point of the split — Next owns the cookie as *transport*, and every
 * authorization decision lives in FastAPI.
 *
 * Being same-origin is what makes the session cookie work at all. A direct call from the
 * browser to :8000 would be cross-origin, which means CORS with credentials, a cookie
 * that cannot carry the `__Host-` prefix, and `SameSite=Lax` refusing to send it. Proxying
 * keeps the cookie first-party and lets the API run with no CORS middleware at all —
 * a permissive origin on a multi-tenant API is a tenant-isolation risk, so having none is
 * better than having one configured carefully.
 *
 * Deliberately absent: any header this proxy invents to prove the request came from the
 * browser. A value manufactured by the server it is meant to protect proves nothing. CSRF
 * is a double-submit token minted by the API and echoed by the page.
 */

/** Where the gateway lives. Server-side only — never `NEXT_PUBLIC_`. */
const API_ORIGIN = process.env.JUTSU_API_URL ?? "http://localhost:8000";

/**
 * Headers that must not be relayed.
 *
 * `host` would make the API think it was addressed directly; the hop-by-hop headers
 * describe *this* connection, not the next one, and forwarding them corrupts framing —
 * `content-length` in particular, because the body is re-encoded here.
 */
const STRIPPED = new Set([
  "host",
  "connection",
  "keep-alive",
  "transfer-encoding",
  "upgrade",
  "content-length",
  "accept-encoding",
]);

async function forward(request: NextRequest, path: string[]): Promise<Response> {
  const target = new URL(`/${path.join("/")}`, API_ORIGIN);
  target.search = request.nextUrl.search;

  const headers = new Headers();
  request.headers.forEach((value, name) => {
    if (!STRIPPED.has(name.toLowerCase())) headers.set(name, value);
  });

  // Read the body rather than streaming it. Streaming would need `duplex: "half"`, which
  // ties this handler to the Node runtime in a way that breaks silently if it is ever
  // moved to the edge. These payloads are small forms; the copy is not worth the trap.
  const body =
    request.method === "GET" || request.method === "HEAD"
      ? undefined
      : await request.arrayBuffer();

  const upstream = await fetch(target, {
    method: request.method,
    headers,
    body,
    // The API issues the session cookie, so redirects must not be followed silently —
    // a 3xx is information the caller asked for.
    redirect: "manual",
    cache: "no-store",
  });

  const response = new NextResponse(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
  });

  upstream.headers.forEach((value, name) => {
    // `set-cookie` is handled separately: a Headers object collapses repeated entries
    // into one comma-joined string, which silently corrupts multiple cookies — and this
    // endpoint always sets two.
    if (name.toLowerCase() !== "set-cookie" && !STRIPPED.has(name.toLowerCase())) {
      response.headers.set(name, value);
    }
  });

  for (const cookie of upstream.headers.getSetCookie()) {
    response.headers.append("set-cookie", cookie);
  }

  return response;
}

type Context = RouteContext<"/api/jutsu/[...path]">;

export async function GET(request: NextRequest, ctx: Context) {
  const { path } = await ctx.params;
  return forward(request, path);
}

export async function POST(request: NextRequest, ctx: Context) {
  const { path } = await ctx.params;
  return forward(request, path);
}

export async function PATCH(request: NextRequest, ctx: Context) {
  const { path } = await ctx.params;
  return forward(request, path);
}

// Added with the connection-policies endpoint, the API's first PUT. A method the
// proxy does not export is a Next-level 405 that never reaches FastAPI — an
// easy-to-misread failure, because the API's own OpenAPI says the route exists.
export async function PUT(request: NextRequest, ctx: Context) {
  const { path } = await ctx.params;
  return forward(request, path);
}

export async function DELETE(request: NextRequest, ctx: Context) {
  const { path } = await ctx.params;
  return forward(request, path);
}
