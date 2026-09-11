import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConsoleNav, type ConsoleNavGroup, type ConsoleNavItem } from "@/components/console-nav";

const route = vi.hoisted(() => ({ pathname: "/admin/employees" }));

vi.mock("next/navigation", () => ({
  usePathname: () => route.pathname,
}));

const ITEMS: ConsoleNavItem[] = [
  {
    href: "/admin/employees",
    name: "Employees",
    description: "The people here.",
    status: "live",
    slice: "P1",
  },
  {
    href: "/admin",
    name: "Overview",
    description: "At a glance.",
    status: "live",
    slice: "P1",
  },
  {
    href: "/admin/audit",
    name: "Audit log",
    description: "Every security-sensitive action.",
    status: "pending",
    slice: "P2",
  },
];

describe("live sections", () => {
  it("are links", () => {
    render(<ConsoleNav items={ITEMS} label="Admin sections" />);

    expect(screen.getByRole("link", { name: "Employees" })).toHaveAttribute(
      "href",
      "/admin/employees",
    );
    expect(screen.getByRole("link", { name: "Overview" })).toBeInTheDocument();
  });

  it("mark the current page for assistive technology", () => {
    render(<ConsoleNav items={ITEMS} label="Admin sections" />);

    expect(screen.getByRole("link", { name: "Employees" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Overview" })).not.toHaveAttribute("aria-current");
  });
});

describe("pending sections", () => {
  it("are NOT links — the whole point of the status field", () => {
    render(<ConsoleNav items={ITEMS} label="Admin sections" />);

    // Listed, so the shape of the product is visible…
    expect(screen.getByText("Audit log")).toBeInTheDocument();
    // …but not a door onto a 404. This is the regression the shared component fixed:
    // four of six admin sections used to render as links to routes that do not exist.
    expect(screen.queryByRole("link", { name: /audit log/i })).not.toBeInTheDocument();
  });

  it("name the slice that delivers them, on screen", () => {
    render(<ConsoleNav items={ITEMS} label="Admin sections" />);

    // Not buried in a tooltip: "when" is a question the reader should not have to hover
    // to answer.
    expect(screen.getByText("P2")).toBeInTheDocument();
  });
});

describe("the nav itself", () => {
  it("is a labelled landmark", () => {
    render(<ConsoleNav items={ITEMS} label="Your console" />);

    expect(screen.getByRole("navigation", { name: "Your console" })).toBeInTheDocument();
  });

  it("renders nothing but list items", () => {
    render(<ConsoleNav items={ITEMS} label="Admin sections" />);

    expect(screen.getAllByRole("listitem")).toHaveLength(ITEMS.length);
  });
});

describe("grouped sections", () => {
  const GROUPS = [
    { label: null, items: [ITEMS[1]] },
    { label: "People", items: [ITEMS[0]] },
    { label: "Operations", items: [ITEMS[2]] },
  ];

  it("renders each group's heading", () => {
    render(<ConsoleNav groups={GROUPS} label="Admin sections" />);

    expect(screen.getByRole("heading", { name: "People" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Operations" })).toBeInTheDocument();
  });

  it("keeps the authored order rather than sorting", () => {
    // The IA is a designed sequence — Overview, then People, then Operations. Sorting it
    // alphabetically would put Operations before People for no reason a reader benefits
    // from, and would silently reorder itself when a group is renamed.
    render(<ConsoleNav groups={GROUPS} label="Admin sections" />);

    const headings = screen.getAllByRole("heading").map((h) => h.textContent);
    expect(headings).toEqual(["People", "Operations"]);
  });

  it("gives the ungrouped run no heading", () => {
    render(<ConsoleNav groups={GROUPS} label="Admin sections" />);

    // Overview sits above the first heading and must not invent one.
    expect(screen.getAllByRole("heading")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Overview" })).toBeInTheDocument();
  });

  it("still renders every item exactly once", () => {
    render(<ConsoleNav groups={GROUPS} label="Admin sections" />);

    expect(screen.getAllByRole("listitem")).toHaveLength(ITEMS.length);
  });

  it("takes precedence over a flat list, rather than rendering both", () => {
    render(<ConsoleNav items={ITEMS} groups={GROUPS} label="Admin sections" />);

    expect(screen.getAllByRole("listitem")).toHaveLength(ITEMS.length);
  });
});

/**
 * The sidebar as its own scroll area.
 *
 * The admin shell is one viewport tall and clips its overflow, and the sidebar used to
 * sit in it with no scroller of its own, so on a laptop screen the Operations group was
 * cut off and unreachable. jsdom lays nothing out, so these give the list a geometry by
 * hand and assert what the component does with it: which way it scrolls, by how much,
 * and when the edge fades show.
 */
describe("the vertical sidebar", () => {
  const SIDEBAR: ConsoleNavGroup[] = [
    { label: null, items: [ITEMS[1]] },
    { label: "People", items: [ITEMS[0]] },
    {
      label: "Operations",
      items: [
        {
          href: "/admin/health",
          name: "System health",
          description: "Whether it is working.",
          status: "live",
          slice: "P1",
        },
      ],
    },
  ];

  /** The list's scroller: the one element inside the landmark that holds every group. */
  const scroller = () => document.querySelector("nav")?.firstElementChild ?? null;

  function rect({ top = 0, bottom = 0, left = 0, right = 0 }: Partial<DOMRect>): DOMRect {
    return {
      top,
      bottom,
      left,
      right,
      x: left,
      y: top,
      width: right - left,
      height: bottom - top,
      toJSON: () => ({}),
    } as DOMRect;
  }

  /** Stand-ins for the layout jsdom does not do, removed again after every test. */
  const patched: string[] = [];
  function patch(name: string, descriptor: PropertyDescriptor) {
    Object.defineProperty(HTMLElement.prototype, name, { configurable: true, ...descriptor });
    patched.push(name);
  }

  afterEach(() => {
    vi.restoreAllMocks();
    for (const name of patched.splice(0)) {
      delete (HTMLElement.prototype as unknown as Record<string, unknown>)[name];
    }
    route.pathname = "/admin/employees";
  });

  it("brings the current section into view when it sits below the fold", () => {
    route.pathname = "/admin/health";
    const scrollTo = vi.fn();
    patch("scrollTo", { value: scrollTo });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (
      this: Element,
    ) {
      if (this === scroller()) return rect({ top: 100, bottom: 400, right: 224 });
      if (this.getAttribute("aria-current") === "page") {
        return rect({ top: 700, bottom: 736, right: 224 });
      }
      return rect({});
    });

    render(<ConsoleNav groups={SIDEBAR} label="Admin sections" />);

    // The list's own scroll position, and only as far as needed: the 336px that bring the
    // current section's bottom edge inside, plus a margin so it does not sit flush.
    expect(scrollTo).toHaveBeenCalledTimes(1);
    expect(scrollTo).toHaveBeenCalledWith({ top: 348, left: 0 });
  });

  it("leaves the list where it is when the current section is already in view", () => {
    // Snapping the list on every render would fight somebody scrolling it themselves.
    const scrollTo = vi.fn();
    patch("scrollTo", { value: scrollTo });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (
      this: Element,
    ) {
      if (this === scroller()) return rect({ top: 100, bottom: 400, right: 224 });
      if (this.getAttribute("aria-current") === "page") {
        return rect({ top: 150, bottom: 186, right: 224 });
      }
      return rect({});
    });

    render(<ConsoleNav groups={SIDEBAR} label="Admin sections" />);

    expect(scrollTo).not.toHaveBeenCalled();
  });

  it("fades the edge where sections are hidden, and clears it at each end", () => {
    // A list three times taller than the room it has.
    let position = 0;
    patch("scrollHeight", { get(this: HTMLElement) { return this === scroller() ? 900 : 0; } });
    patch("clientHeight", { get(this: HTMLElement) { return this === scroller() ? 300 : 0; } });
    patch("scrollTop", {
      get(this: HTMLElement) {
        return this === scroller() ? position : 0;
      },
      set() {},
    });

    render(<ConsoleNav groups={SIDEBAR} label="Admin sections" />);
    const nav = screen.getByRole("navigation", { name: "Admin sections" });
    const fade = (edge: "above" | "below") => nav.querySelector(`[data-fade="${edge}"]`);

    // At the top: more below, nothing above.
    expect(fade("above")).toHaveAttribute("data-visible", "false");
    expect(fade("below")).toHaveAttribute("data-visible", "true");

    position = 300;
    fireEvent.scroll(scroller()!);
    expect(fade("above")).toHaveAttribute("data-visible", "true");
    expect(fade("below")).toHaveAttribute("data-visible", "true");

    // At the end: the last section is on screen, so nothing says "more".
    position = 600;
    fireEvent.scroll(scroller()!);
    expect(fade("above")).toHaveAttribute("data-visible", "true");
    expect(fade("below")).toHaveAttribute("data-visible", "false");

    // Decoration, not content: a screen reader never meets it.
    expect(fade("below")).toHaveAttribute("aria-hidden", "true");
  });

  it("adds no edge fades to the horizontal strip", () => {
    // The strip is one row tall; its own sideways scrollbar is the affordance.
    render(<ConsoleNav items={ITEMS} label="Your console" orientation="horizontal" />);

    const nav = screen.getByRole("navigation", { name: "Your console" });
    expect(nav.querySelector("[data-fade]")).toBeNull();
  });
});
