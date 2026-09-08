import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CopyButton } from "@/components/copy-button";

/**
 * Copying a value, and telling the truth about whether it worked.
 *
 * The bug this component was written for: every call site fired
 * `void navigator.clipboard.writeText(value)` and then reported success unconditionally.
 * `writeText` rejects on an insecure origin, in an unfocused document and whenever the
 * user has denied clipboard access — so the confirmation was a claim about a promise
 * nobody read, and the reader found out by pasting nothing into a sign-in field.
 */

function stubClipboard(writeText: () => Promise<void>) {
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: vi.fn(writeText) } });
  return navigator.clipboard.writeText as ReturnType<typeof vi.fn>;
}

describe("when the clipboard accepts it", () => {
  it("writes the exact value", async () => {
    const writeText = stubClipboard(async () => {});
    render(<CopyButton value="JUTSU-KT-4RCXQ2" label="Copy KT ID" />);

    await userEvent.click(screen.getByRole("button", { name: "Copy KT ID" }));

    expect(writeText).toHaveBeenCalledWith("JUTSU-KT-4RCXQ2");
  });

  it("confirms on the button and announces it once", async () => {
    stubClipboard(async () => {});
    render(<CopyButton value="JUTSU-KT-4RCXQ2" label="Copy KT ID" />);

    const button = screen.getByRole("button", { name: "Copy KT ID" });
    await userEvent.click(button);

    expect(button).toHaveTextContent("Copied");
    expect(screen.getByRole("status")).toHaveTextContent("Copy KT ID: copied.");
    // The accessible name is unchanged, so the control is not re-announced as a
    // different button while the reader still has it focused.
    expect(button).toHaveAccessibleName("Copy KT ID");
  });

  it("says nothing at rest", () => {
    render(<CopyButton value="X" label="Copy" />);

    // An empty live region is what stops a screen reader repeating the previous result
    // on every unrelated re-render of the row this sits in.
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("does not put the value in the page a second time", () => {
    // It almost always sits beside the value it copies. A hidden duplicate would make a
    // screen reader read somebody's JUTSU ID out, and then read it out again.
    render(
      <p>
        JUTSU-EMP-7KQ2 <CopyButton value="JUTSU-EMP-7KQ2" label="Copy JUTSU ID" />
      </p>,
    );

    expect(screen.getAllByText(/JUTSU-EMP-7KQ2/)).toHaveLength(1);
  });
});

describe("when the clipboard refuses", () => {
  it("says so instead of claiming success", async () => {
    stubClipboard(async () => {
      throw new DOMException("Write permission denied.", "NotAllowedError");
    });
    render(<CopyButton value="JUTSU-KT-4RCXQ2" label="Copy KT ID" />);

    const button = screen.getByRole("button", { name: "Copy KT ID" });
    await userEvent.click(button);

    expect(button).toHaveTextContent("Copy failed");
    expect(screen.getByRole("status")).toHaveTextContent("could not copy");
  });

  it("leaves the value selected so the reader's own shortcut works", async () => {
    stubClipboard(async () => {
      throw new Error("denied");
    });
    render(<CopyButton value="JUTSU-KT-4RCXQ2" label="Copy KT ID" />);

    await userEvent.click(screen.getByRole("button", { name: "Copy KT ID" }));

    // A dead end would mean transcribing eight characters by eye. jsdom implements
    // selection ranges, so this is the real behaviour rather than a spy on it — and the
    // value is now on screen, which is the half that helps a sighted reader.
    expect(screen.getByText("JUTSU-KT-4RCXQ2")).toBeInTheDocument();
    expect(window.getSelection()?.toString()).toBe("JUTSU-KT-4RCXQ2");
  });

  it("survives a browser with no clipboard at all", async () => {
    // On an insecure origin `navigator.clipboard` is undefined, and
    // `navigator.clipboard?.writeText(v)` then evaluates to `undefined` — which `await`
    // resolves, so the obvious spelling reports "Copied" on exactly the browsers that
    // copied nothing. This is the test that caught it.
    vi.stubGlobal("navigator", { ...navigator, clipboard: undefined });
    render(<CopyButton value="X" label="Copy" />);

    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    expect(screen.getByRole("button", { name: "Copy" })).toHaveTextContent("Copy failed");
  });
});
