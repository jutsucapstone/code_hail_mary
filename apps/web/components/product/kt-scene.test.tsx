import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { KtScene } from "@/components/product/kt-scene";

/**
 * The KT column: the robot, or nothing — never a stand-in mark.
 *
 * The column used to wear the JUTSU mark as a poster until the scene drew, and a mark
 * on the robot's chest after. Both are gone by request, and what is pinned here is that
 * neither comes back: whatever the column shows is the robot and only the robot.
 *
 * jsdom implements no `matchMedia`, so every render would answer "not wide enough" and
 * produce nothing; the two queries are answered explicitly instead. The robot itself is
 * replaced by a marker — this suite is about when it is shown, not how it draws.
 */

vi.mock("@/components/ui/spline-robot", () => ({
  SplineRobot: ({ scene, className }: { scene: string; className?: string }) => (
    <div data-testid="spline-robot" data-scene={scene} className={className} />
  ),
}));

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

afterEach(() => vi.unstubAllGlobals());

describe("the robot column", () => {
  it("shows the robot, from the self-hosted scene, when there is room and motion is welcome", () => {
    stubMedia({ wide: true, reduced: false });
    const { container } = render(<KtScene />);

    expect(screen.getByTestId("spline-robot")).toHaveAttribute(
      "data-scene",
      "/spline/kt-robot.splinecode",
    );
    // Nothing but the robot: no logo, no placeholder mark, no loader.
    expect(container.querySelectorAll("img, svg")).toHaveLength(0);
  });

  it("downloads nothing when motion is unwelcome, and puts no stand-in in its place", () => {
    stubMedia({ wide: true, reduced: true });
    const { container } = render(<KtScene />);

    expect(screen.queryByTestId("spline-robot")).toBeNull();
    expect(container.querySelectorAll("img, svg")).toHaveLength(0);
  });

  it("renders nothing at all below the breakpoint, where the page has no column", () => {
    stubMedia({ wide: false, reduced: false });
    const { container } = render(<KtScene />);

    expect(container).toBeEmptyDOMElement();
  });
});
