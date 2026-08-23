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
  // Cloud Run runs this as a container, and the default build expects the whole
  // node_modules tree beside it. "standalone" emits a self-contained server with only
  // the files actually reached, which is the difference between a ~1GB image and a
  // small one — and this repo has already had one argument about deploy weight.
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "SAMEORIGIN" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
