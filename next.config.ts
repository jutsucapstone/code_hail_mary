import path from "node:path";

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // A stray package-lock.json in the user profile above this folder makes
  // Turbopack infer C:\Users\<user> as the workspace root. Pin it to the app.
  turbopack: {
    root: path.resolve(import.meta.dirname),
  },
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
