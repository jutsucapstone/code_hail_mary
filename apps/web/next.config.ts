import path from "node:path";

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Pin the workspace root to the monorepo root, two levels up.
  //
  // Two reasons it cannot be inferred: a stray package-lock.json in the user
  // profile above the repo makes Turbopack guess C:\Users\<user>, and with pnpm's
  // hoisted linker `next` itself resolves from the root node_modules — pinning
  // this at apps/web makes the build fail with "Could not find the Next.js package".
  turbopack: {
    root: path.resolve(import.meta.dirname, "../.."),
  },
  // Cloud Run runs this as a container, and the default build expects the source tree
  // beside it. "standalone" emits a server entrypoint with its own traced copy of what
  // the app reaches at runtime, which is what the image actually ships.
  //
  // It does not, on its own, produce a small image here: in a pnpm workspace the traced
  // node_modules is unusable (see web.Dockerfile), so the runtime tree comes from
  // `pnpm deploy` instead and the image is ~760MB. Most of that is next itself plus its
  // platform swc binary, neither of which is safe to strip by hand.
  output: "standalone",

  // Without this the tracer roots at apps/web and stops there, so nothing above it is
  // copied. In a pnpm workspace almost everything real lives above it — dependencies
  // resolve through the root node_modules and the .pnpm store — and the container dies
  // at startup on MODULE_NOT_FOUND for a transitive package like @swc/helpers.
  //
  // Found by running the built image, not by reading the build: `next build` reports
  // success either way, because the files are missing from the *output*, not the build.
  outputFileTracingRoot: path.resolve(import.meta.dirname, "../.."),
  poweredByHeader: false,
  reactStrictMode: true,
  async headers() {
    const headers = [
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
      { key: "X-Frame-Options", value: "SAMEORIGIN" },
      {
        key: "Permissions-Policy",
        value: "camera=(), microphone=(), geolocation=()",
      },
    ];

    // **HSTS, and only on a production build.**
    //
    // This is the origin a browser actually talks to: every API call goes through the
    // same-origin proxy at `/api/jutsu/*`, so the session cookie and every request the
    // product makes are covered by the policy set here. The API's own `*.a.run.app`
    // host is never a browser origin for a user, and it sits under a Google-owned
    // domain where `includeSubDomains` would not be ours to assert.
    //
    // `includeSubDomains` is included on evidence rather than by reflex: the deployment
    // has exactly two hostnames, `jutsu.co.in` (200) and `www.jutsu.co.in` (301), and
    // both serve HTTPS — checked, not assumed. Everything is behind Cloud Run with
    // Google-managed certificates, so a future subdomain arrives HTTPS-first too.
    //
    // `preload` is deliberately absent. It requires submission to a browser-shipped
    // list and is slow and awkward to reverse, which makes it a decision about the
    // domain rather than about this file.
    //
    // Guarded on the build so `next dev` over `http://localhost:3210` never sends it.
    // A browser ignores the header on a plain-HTTP response anyway (RFC 6797 §7.2), so
    // this is about not asserting a policy the dev server cannot honour rather than
    // about a live risk.
    if (process.env.NODE_ENV === "production") {
      headers.push({
        key: "Strict-Transport-Security",
        value: "max-age=31536000; includeSubDomains",
      });
    }

    return [{ source: "/:path*", headers }];
  },
};

export default nextConfig;
