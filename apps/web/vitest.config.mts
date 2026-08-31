import { basename } from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vitest/config";

/**
 * Component tests for the surfaces that talk to the API.
 *
 * Deliberately jsdom and not a browser: what these tests assert is what the client
 * *sends* and how it renders each documented status, and neither needs a real engine.
 * An E2E stack would also need a running API, a database and — for search — a paid
 * Vertex call per assertion, which is exactly the thing the fake keeps out of CI.
 *
 * `fetch` is stubbed per test rather than by a service worker. The API client is one
 * `fetch` call behind `lib/api.ts`, so a stub there is the whole seam, and it lets a test
 * assert the request body — which is how "the browser never sends a tenant field" is
 * checked rather than asserted in a comment.
 */
/**
 * Make a static image import look the way Next makes it look.
 *
 * `import logoSrc from "@/public/jutsu-logo.png"` is a Next build feature: the loader
 * turns it into a `StaticImageData` object carrying `src`, `width` and `height`, and
 * `next/image` *requires* those dimensions — it throws `missing required "width"` without
 * them. Vite has no such loader, so the import arrived as a bare URL string and every
 * test that rendered the console header failed on the logo rather than on anything the
 * test was about.
 *
 * The dimensions here are deliberately arbitrary. jsdom lays nothing out, so no assertion
 * can depend on them; what matters is that the shape satisfies the contract, so the real
 * `next/image` code path runs instead of being mocked away.
 */
function staticImageImports(): Plugin {
  const IMAGE = /\.(png|jpe?g|gif|webp|avif|svg)$/;

  return {
    name: "jutsu:static-image-imports",
    enforce: "pre",
    load(id) {
      const file = id.split("?")[0];
      if (!IMAGE.test(file)) return null;
      const src = `/${basename(file)}`;
      return `export default ${JSON.stringify({ src, width: 512, height: 512 })};`;
    },
  };
}

export default defineConfig({
  plugins: [react(), staticImageImports()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["node_modules/**", ".next/**"],
  },
});
