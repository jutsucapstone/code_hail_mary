import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ConsoleNav, type ConsoleNavItem } from "@/components/console-nav";

vi.mock("next/navigation", () => ({
  usePathname: () => "/admin/employees",
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
