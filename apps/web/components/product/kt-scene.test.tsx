import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The KT figure, and what stands in its place.
 *
 * This component had no tests, and the reason is the bug: jsdom implements no
 * `matchMedia`, so every render answered "not wide enough" and produced nothing at all
 * — a suite full of assertions about an empty div would have proved the panel worked
 * while it was rendering emptiness in a browser too.
 *
 * The panel is decorative (`aria-hidden`), so what is asserted here is not what a
 * reader is told but what the LAYOUT does: a `lg` grid whose second column is empty
 * reads as a page that failed to load, and that state is reachable from an ordinary
 * accessibility setting.
 */

const WIDE = "(min-width: 1024px)";
const REDUCED = "(prefers-reduced-motion: reduce)";

/** Answer the two queries this component asks, and nothing else. */
function stubMedia({ wide, reduced }: { wide: boolean; reduced: boolean }) {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        matches: query.includes(WIDE) ? wide : query.includes(REDUCED) ? reduced : false,
        media: query,
        onchange: null,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

/**
 * Load the component AFTER the stub is in place.
 *
 * `kt-scene` caches its `MediaQueryList` at module scope (`wideMq ??= …`), which is
 * right in a browser — `matchMedia` is stable there — and fatal to a suite that changes
 * the answer between cases: the first import would pin "wide" for every test that
 * followed, and the reduced-motion case would quietly assert against the previous
 * one's media. Resetting the registry per test is what makes each case independent.
 */
async function mount(media: { wide: boolean; reduced: boolean }) {
  stubMedia(media);
  vi.resetModules();
  const { KtScene } = await import("@/components/product/kt-scene");
  return render(<KtScene />);
}

beforeEach(() => vi.resetModules());
afterEach(() => vi.unstubAllGlobals());

describe("the second column", () => {
  it("stands in with a still while the scene has not drawn", async () => {
    // The ordinary case, and the one a cold cache spends seconds in: 1.3MB of scene is
    // in flight and the column must not be blank while it travels.
    await mount({ wide: true, reduced: false });

    expect(screen.getByTestId("kt-still")).toBeInTheDocument();
  });

  it("keeps the still when motion is unwelcome, rather than emptying the column", async () => {
    // The reported bug. `prefers-reduced-motion` is a Windows default under
    // "Animation effects: off", and the panel used to answer it with `return null` —
    // half a page of nothing beside the form.
    const { container } = await mount({ wide: true, reduced: true });

    expect(screen.getByTestId("kt-still")).toBeInTheDocument();
    // Scoped to this render: `document` still holds the previous case's tree until
    // cleanup, so a document-wide query here passes or fails on test ORDER.
    expect(container.querySelector("spline-viewer")).toBeNull();
  });

  it("downloads the scene only when motion is welcome", async () => {
    // The accessibility win this fix must not undo: the still is not a reason to start
    // fetching a 3D scene for somebody who asked for no animation.
    const { container } = await mount({ wide: true, reduced: false });

    expect(container.querySelector("spline-viewer")).not.toBeNull();
  });

  it("renders nothing at all below the breakpoint", async () => {
    // There is no column to stand in for — the page hides the panel outright — so a
    // still here would be weight nobody sees.
    const { container } = await mount({ wide: false, reduced: false });

    expect(container).toBeEmptyDOMElement();
  });
});
