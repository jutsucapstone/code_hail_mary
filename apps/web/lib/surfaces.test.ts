import { existsSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { SURFACES } from "@/lib/surfaces";

/**
 * `lib/surfaces.ts` promises that a surface cannot be routed without being in the
 * console's nav, or listed without a route. The list also feeds the middleware's gate,
 * so a page whose entry is gone is served without the sign-in redirect, and an entry
 * whose page is gone is a nav link to a 404. This reads the route tree itself.
 *
 * The path is built from the file path, not `new URL("../app/…", import.meta.url)`:
 * under jsdom Vite rewrites that expression into an asset URL, which is not a file URL.
 */
const PRODUCT_ROUTES = join(dirname(fileURLToPath(import.meta.url)), "..", "app", "(product)");

function routedSlugs(): string[] {
  return readdirSync(PRODUCT_ROUTES, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .filter((entry) => existsSync(join(PRODUCT_ROUTES, entry.name, "page.tsx")))
    .map((entry) => entry.name)
    .sort();
}

describe("product surfaces", () => {
  it("routes exactly the surfaces the console lists, and nothing else", () => {
    expect(routedSlugs()).toEqual(SURFACES.map((surface) => surface.slug).sort());
  });
});
