import { createElement, StrictMode } from "react";
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { pointWasmAtThisOrigin, SplineScene } from "@/components/ui/spline-scene";

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
 * CDN fetch, refused by CSP, an empty column — silently, exactly the bug being fixed
 * here. A red test is the only thing standing between that and another silent
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

/**
 * Teardown. The viewer, once removed from the page, kept rendering into a 0×0 canvas
 * — ~280 WebGPU errors a second, for good — so every unmount must reach its `unload()`.
 * jsdom has no WebGL; a stand-in element with an `unload` spy is all these need.
 */
class FakeViewer extends HTMLElement {
  unload = vi.fn();
}
if (!customElements.get("spline-viewer")) customElements.define("spline-viewer", FakeViewer);

function mountScene(strict = false) {
  const scene = createElement(SplineScene, { scene: "/spline/kt-robot.splinecode" });
  const view = render(strict ? createElement(StrictMode, null, scene) : scene);
  const viewer = view.container.querySelector("spline-viewer");
  if (!(viewer instanceof FakeViewer)) throw new Error("the scene rendered no viewer");
  return { ...view, viewer };
}

describe("unloading the scene", () => {
  it("unloads it when the component is removed", () => {
    const { viewer, unmount } = mountScene();

    unmount();

    expect(viewer.unload).toHaveBeenCalledTimes(1);
  });

  it("also unloads a load that finishes after the component is gone", async () => {
    // `unload()` is a no-op until the scene has loaded, so a column that unmounts mid-
    // load would otherwise finish into a detached element and leak exactly as before.
    const { viewer, unmount } = mountScene();
    unmount();

    viewer.dispatchEvent(new CustomEvent("load-complete"));
    await Promise.resolve();

    expect(viewer.unload).toHaveBeenCalledTimes(2);
  });

  it("leaves a scene on screen alone when StrictMode re-runs effects in development", () => {
    // StrictMode unmounts and remounts effects without removing the node; unloading
    // then would blank a robot that is still showing.
    const { viewer } = mountScene(true);

    expect(viewer.unload).not.toHaveBeenCalled();
  });
});
