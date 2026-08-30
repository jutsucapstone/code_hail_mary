import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

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
export default defineConfig({
  plugins: [react()],
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
