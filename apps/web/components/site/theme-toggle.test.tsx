import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeToggle } from "@/components/site/theme-toggle";

/**
 * The theme control, driven by keyboard only.
 *
 * A radiogroup with a roving tabindex has exactly one tab stop, so Tab reaches the
 * selected option and stops. Without arrow keys the other two were unreachable by
 * keyboard at all — WCAG 2.1.1, on the control that decides whether the product is
 * legible to someone who needs a dark or a light ground.
 */

const theme = vi.hoisted(() => ({ current: "system", setTheme: vi.fn() }));

vi.mock("next-themes", () => ({
  useTheme: () => ({ theme: theme.current, setTheme: theme.setTheme }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  theme.current = "system";
});

describe("reaching every option from the keyboard", () => {
  it("moves forward with ArrowRight and wraps", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    const system = screen.getByRole("radio", { name: "System" });
    system.focus();

    await user.keyboard("{ArrowRight}");
    // System is last, so forward wraps to the first option.
    expect(theme.setTheme).toHaveBeenCalledWith("light");
  });

  it("moves backward with ArrowLeft", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    screen.getByRole("radio", { name: "System" }).focus();
    await user.keyboard("{ArrowLeft}");

    expect(theme.setTheme).toHaveBeenCalledWith("dark");
  });

  it("treats Down and Up like Right and Left", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    screen.getByRole("radio", { name: "System" }).focus();
    await user.keyboard("{ArrowDown}");
    expect(theme.setTheme).toHaveBeenCalledWith("light");

    // Focus followed the selection to Light, so Up from there wraps to System — the
    // movement is relative to where focus now is, not to where it started.
    await user.keyboard("{ArrowUp}");
    expect(theme.setTheme).toHaveBeenLastCalledWith("system");
  });

  it("moves focus with the selection, so the next arrow continues from there", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    screen.getByRole("radio", { name: "System" }).focus();
    await user.keyboard("{ArrowRight}");

    expect(screen.getByRole("radio", { name: "Light" })).toHaveFocus();
  });

  it("leaves other keys to the browser", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    screen.getByRole("radio", { name: "System" }).focus();
    await user.keyboard("{Tab}");

    expect(theme.setTheme).not.toHaveBeenCalled();
  });

  it("is one tab stop, which is what makes the arrow keys necessary", () => {
    theme.current = "dark";
    render(<ThemeToggle />);

    const tabbable = screen
      .getAllByRole("radio")
      .filter((node) => node.getAttribute("tabindex") === "0");

    expect(tabbable).toHaveLength(1);
  });
});
