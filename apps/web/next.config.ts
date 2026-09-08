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
    const isProduction = process.env.NODE_ENV === "production";

    // **One policy for the whole app, and the reason is App Router navigation.**
    //
    // This was briefly two rules — a strict one everywhere and a looser one on
    // `/handover`, whose Spline scene loads a pinned ES module from unpkg. That does not
    // work, and the way it fails is invisible: a Content-Security-Policy is a property of
    // the DOCUMENT that carried the header, not of the current route. `/handover` is
    // reached from the KT shell through `next/link` (components/kt/kt-shell.tsx), which is
    // a same-document navigation, so the page inherits whatever policy the entry document
    // was served with. The scene would therefore render on a hard load and be blocked on
    // the normal in-app path — protection that depends on how the reader arrived is worse
    // than none, because nobody can reason about it.
    //
    // So the CDN origin is allowed everywhere and stated plainly rather than hidden
    // behind a rule that does not hold. **The change that would let this tighten is
    // vendoring `spline-viewer.js` into `public/`** — one self-contained file, after which
    // `script-src` drops to `'self' 'unsafe-inline'` and the supply-chain exposure goes
    // with it.
    //
    // `'unsafe-inline'` on `script-src` is a limitation, not an oversight. Next 16 needs
    // it unless every page is dynamically rendered behind a nonce — the bundled docs are
    // explicit that nonces disable static generation, CDN caching and PPR — or unless the
    // experimental SRI flag is enabled, which is not a thing to put under a production
    // release. The directives that do NOT need inline script are all strict, and
    // `connect-src` is the one that matters most here: every API call goes through the
    // same-origin proxy at `/api/jutsu/*`, so the only third-party origin it names is the
    // storage host below, and injected script has nowhere else to exfiltrate to.

    // Where a Knowledge Basket file's bytes actually live. V4 signed URLs are issued
    // against the global `storage.googleapis.com` endpoint rather than a bucket-named
    // host, so this one origin covers upload, download and preview for every bucket and
    // needs no `JUTSU_BASKET_BUCKET` at build time.
    const STORAGE_ORIGIN = "https://storage.googleapis.com";

    const csp = [
      "default-src 'self'",
      // `'unsafe-eval'` only in development: React uses `eval` there to reconstruct
      // server-side error stacks in the browser. Neither React nor Next uses it in a
      // production build.
      `script-src 'self' 'unsafe-inline' https://unpkg.com${isProduction ? "" : " 'unsafe-eval'"}`,
      // Next inlines critical CSS, and Tailwind v4's output is a stylesheet rather than
      // inline styles — but the framework's own injection is what forces this.
      "style-src 'self' 'unsafe-inline'",
      // `blob:` and `data:` are how the app renders locally-generated images. The storage
      // origin is here for Knowledge Basket previews, which render an uploaded image
      // straight from a short-lived signed URL rather than proxying the bytes through
      // Cloud Run.
      `img-src 'self' blob: data: ${STORAGE_ORIGIN}`,
      // `next/font/google` downloads and self-hosts at build time, so no font CDN is
      // ever contacted at runtime.
      "font-src 'self'",
      // `connect-src` carries the storage origin because a Knowledge Basket upload PUTs
      // its bytes straight to Cloud Storage under a signed URL (ADR 0020). Without this
      // exact origin the browser blocks the request and the whole feature is dead in
      // production while working perfectly in every test — a scripted `fetch` has no CSP.
      //
      // Exactly one host, never `https:` and never a wildcard: the point of a narrow
      // `connect-src` is that injected script has nowhere to exfiltrate to, and one more
      // named origin costs that guarantee nothing.
      `connect-src 'self' https://unpkg.com ${STORAGE_ORIGIN}`,
      "worker-src 'self' blob:",
      "manifest-src 'self'",
      // Audio and video the employee uploaded, played from the same signed URL. These
      // are `stored` files — kept and playable, never transcribed.
      `media-src 'self' ${STORAGE_ORIGIN}`,
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      // Kept in step with `X-Frame-Options: SAMEORIGIN` below on purpose: two headers
      // that disagree about framing is how one of them silently stops being the answer.
      "frame-ancestors 'self'",
      "frame-src 'none'",
      ...(isProduction ? ["upgrade-insecure-requests"] : []),
    ].join("; ");

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
    if (isProduction) {
      headers.push({
        key: "Strict-Transport-Security",
        value: "max-age=31536000; includeSubDomains",
      });
    }

    const cspHeader = { key: "Content-Security-Policy", value: csp };

    return [{ source: "/:path*", headers: [...headers, cspHeader] }];
  },
};

export default nextConfig;
