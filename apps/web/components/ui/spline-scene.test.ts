import { describe, expect, it } from "vitest";

import { pointWasmAtThisOrigin } from "@/components/ui/spline-scene";

/**
 * The WASM redirect, and the seam it depends on.
 *
 * The robot was invisible in production because the runtime fetched `process.wasm`
 * from `cdn.spline.design`, which `connect-src` has never allowed. The fix passes the
 * runtime's own documented `wasmPath` option by wrapping the element's application
 * factory — a private method on a pinned version.
 *
 * That is a real dependency on somebody else's internals, so the point of these tests
 * is not that the wrapper works (it obviously does) but that its DISAPPEARANCE is
 * loud. `_createApplication` going away on a version bump would fail open: no patch,
 * CDN fetch, refused by CSP, panel keeps its still — silently, exactly the bug being
 * fixed here. A red test is the only thing standing between that and another silent
 * regression.
 */

function fakeConstructor(withFactory = true) {
  class Fake {
    _createApplication() {
      return { name: "app" } as Record<string, unknown>;
    }
  }
  if (!withFactory) {
    delete (Fake.prototype as unknown as Record<string, unknown>)._createApplication;
  }
  return Fake;
}

describe("pointing the runtime's wasm at this origin", () => {
  it("makes the application resolve wasm from /spline", () => {
    const Fake = fakeConstructor();

    expect(pointWasmAtThisOrigin(Fake)).toBe(true);
    const app = new Fake()._createApplication() as Record<string, unknown>;
    // No trailing slash: the runtime strips them, and a doubled separator would 404.
    expect(app._wasmPath).toBe("/spline");
  });

  it("reports failure when the hook point is gone, rather than pretending", () => {
    // The version-bump case. Returning false is what a future reader needs to see;
    // silently doing nothing is how this bug shipped the first time.
    expect(pointWasmAtThisOrigin(fakeConstructor(false))).toBe(false);
    expect(pointWasmAtThisOrigin(undefined)).toBe(false);
    expect(pointWasmAtThisOrigin({})).toBe(false);
  });

  it("is idempotent, because the module may be imported more than once", () => {
    const Fake = fakeConstructor();

    expect(pointWasmAtThisOrigin(Fake)).toBe(true);
    expect(pointWasmAtThisOrigin(Fake)).toBe(true);

    const app = new Fake()._createApplication() as Record<string, unknown>;
    expect(app._wasmPath).toBe("/spline");
    expect(app.name).toBe("app"); // the original factory still ran
  });

  it("keeps whatever the real factory returned", () => {
    // The wrapper adds a property; it must not replace the application.
    const Fake = fakeConstructor();
    pointWasmAtThisOrigin(Fake);

    const app = new Fake()._createApplication() as Record<string, unknown>;

    expect(app.name).toBe("app");
  });
});
