import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CodeInput } from "@/components/pilot/code-input";

/**
 * The six-digit field, tested through the keyboard rather than through its internals.
 *
 * Every assertion here is a behaviour the mandate names — paste, auto-advance, backspace,
 * the numeric keyboard, auto-submit — and each one is checked against what a person can
 * observe: the painted boxes, the value the form would submit, and the callback. The
 * point of the one-input design is that these come from the browser; the tests exist to
 * catch the day somebody "improves" it into six inputs and quietly loses paste.
 */

function slots() {
  return Array.from({ length: 6 }, (_, index) => screen.getByTestId(`code-slot-${index}`));
}

function field() {
  return screen.getByLabelText("Six-digit code") as HTMLInputElement;
}

describe("the six boxes", () => {
  it("paints one box per digit, empty to begin with", () => {
    render(<CodeInput id="code" name="code" label="Six-digit code" />);

    expect(slots()).toHaveLength(6);
    expect(slots().every((slot) => slot.textContent === "")).toBe(true);
  });

  it("fills the boxes in order as the digits are typed", async () => {
    const user = userEvent.setup();
    render(<CodeInput id="code" name="code" label="Six-digit code" />);

    await user.click(field());
    await user.keyboard("428");

    expect(slots()[0]).toHaveTextContent("4");
    expect(slots()[1]).toHaveTextContent("2");
    expect(slots()[2]).toHaveTextContent("8");
    expect(slots()[3]).toHaveTextContent("");
    expect(field().value).toBe("428");
  });

  it("marks a filled box so the fill state is not carried by colour alone", async () => {
    const user = userEvent.setup();
    render(<CodeInput id="code" name="code" label="Six-digit code" />);

    await user.click(field());
    await user.keyboard("4");

    expect(slots()[0]).toHaveAttribute("data-filled", "true");
    expect(slots()[1]).toHaveAttribute("data-filled", "false");
  });
});

describe("what a person actually does with a code", () => {
  it("accepts a paste of the whole code", async () => {
    const user = userEvent.setup();
    render(<CodeInput id="code" name="code" label="Six-digit code" />);

    await user.click(field());
    await user.paste("204815");

    expect(field().value).toBe("204815");
    expect(slots().map((slot) => slot.textContent).join("")).toBe("204815");
  });

  it("survives a code pasted with the punctuation people copy along with it", async () => {
    const user = userEvent.setup();
    render(<CodeInput id="code" name="code" label="Six-digit code" />);

    await user.click(field());
    await user.paste(" 204-815 ");

    expect(field().value).toBe("204815");
  });

  it("ignores letters, so a typo cannot occupy a box", async () => {
    const user = userEvent.setup();
    render(<CodeInput id="code" name="code" label="Six-digit code" />);

    await user.click(field());
    await user.keyboard("2a0b4");

    expect(field().value).toBe("204");
  });

  it("walks backwards on backspace", async () => {
    const user = userEvent.setup();
    render(<CodeInput id="code" name="code" label="Six-digit code" />);

    await user.click(field());
    await user.keyboard("2048");
    await user.keyboard("{Backspace}{Backspace}");

    expect(field().value).toBe("20");
    expect(slots()[2]).toHaveTextContent("");
  });

  it("stops at six digits rather than silently dropping the last one typed", async () => {
    const user = userEvent.setup();
    render(<CodeInput id="code" name="code" label="Six-digit code" />);

    await user.click(field());
    await user.keyboard("2048159");

    expect(field().value).toBe("204815");
  });

  it("announces completion once, on the sixth digit", async () => {
    const user = userEvent.setup();
    const onComplete = vi.fn();
    render(
      <CodeInput id="code" name="code" label="Six-digit code" onComplete={onComplete} />,
    );

    await user.click(field());
    await user.keyboard("20481");
    expect(onComplete).not.toHaveBeenCalled();

    await user.keyboard("5");
    expect(onComplete).toHaveBeenCalledExactlyOnceWith("204815");
  });
});

describe("the parts a phone and a screen reader need", () => {
  it("asks for the numeric keyboard and offers the platform's one-time code", () => {
    render(<CodeInput id="code" name="code" label="Six-digit code" />);

    expect(field()).toHaveAttribute("inputMode", "numeric");
    expect(field()).toHaveAttribute("autocomplete", "one-time-code");
    expect(field()).toHaveAttribute("pattern", "[0-9]{6}");
  });

  it("is one labelled field, not six unlabelled ones", () => {
    render(<CodeInput id="code" name="code" label="Six-digit code" />);

    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(field()).toHaveAccessibleDescription(/paste the whole code/i);
  });

  it("marks itself rejected and points at the page's one message", () => {
    render(
      <>
        <CodeInput
          id="code"
          name="code"
          label="Six-digit code"
          invalid
          describedBy="verify-error"
        />
        <p id="verify-error">That code is not right. Two attempts left.</p>
      </>,
    );

    expect(field()).toHaveAttribute("aria-invalid", "true");
    // The message lives once, on the page, so a rejection is announced once rather
    // than by both an alert here and an alert there.
    expect(field()).toHaveAccessibleDescription(/two attempts left/i);
  });
});
