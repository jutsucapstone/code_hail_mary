import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { VERIFY_ADDRESS_KEY, setHandoff, useHandoff, useLinkToken } from "@/lib/link-token";

/**
 * Reading the values that arrive with a person rather than with a request.
 *
 * The property under test is where these values are NOT: a token in a query string is
 * transmitted on every request for that URL and lands verbatim in the Cloud Run request
 * log, retained thirty days. Live invitation tokens — a single-factor account-creation
 * credential — and registrants' work email addresses were both found there in production.
 *
 * jsdom gives a real `location`, `history` and `sessionStorage`, so these exercise the
 * actual browser behaviour rather than a mock of it.
 */

function visit(href: string): void {
  window.history.replaceState(null, "", href);
}

afterEach(() => {
  visit("/");
  try {
    window.sessionStorage.clear();
  } catch {
    // Nothing to clear.
  }
});

describe("reading a token from a link", () => {
  it("reads it from the fragment, which the browser never transmits", () => {
    visit("/pilot/accept#token=abcdef0123456789");

    const { result } = renderHook(() => useLinkToken());

    expect(result.current).toBe("abcdef0123456789");
  });

  it("removes it from the address bar once read", async () => {
    // Out of anything the person copies, bookmarks or screenshots — and without adding a
    // history entry, so Back still goes where they expect.
    visit("/pilot/accept?flow=register#token=abcdef0123456789");

    const { result } = renderHook(() => useLinkToken());

    await waitFor(() => expect(window.location.hash).toBe(""));
    expect(window.location.search).toBe("?flow=register");
    // The value survives the scrub: it was captured at first render, so the form still
    // has it after the URL has been rewritten.
    expect(result.current).toBe("abcdef0123456789");
  });

  it("still accepts a token in the query string, for links already in inboxes", async () => {
    // Refusing these would break every invitation sent before the fragment form shipped.
    // A token in a log is a thing to stop repeating, not a reason to lock somebody out of
    // mail they already hold.
    visit("/pilot/accept?token=legacy0123456789");

    const { result } = renderHook(() => useLinkToken());

    expect(result.current).toBe("legacy0123456789");
    // And it is scrubbed from the URL just the same.
    await waitFor(() => expect(window.location.search).toBe(""));
  });

  it("prefers the fragment when a URL somehow carries both", () => {
    visit("/pilot/accept?token=fromquery0123456#token=fromfragment0123");

    const { result } = renderHook(() => useLinkToken());

    expect(result.current).toBe("fromfragment0123");
  });

  it("is an empty string when there is no token at all", () => {
    visit("/pilot/accept");

    const { result } = renderHook(() => useLinkToken());

    expect(result.current).toBe("");
  });

  it("leaves an unrelated URL alone", async () => {
    visit("/pilot/accept?flow=register");

    renderHook(() => useLinkToken());

    await waitFor(() => expect(window.location.search).toBe("?flow=register"));
  });
});

describe("handing an address to the next screen", () => {
  it("carries it without putting it in a URL", () => {
    setHandoff(VERIFY_ADDRESS_KEY, "ada@example.com");

    const { result } = renderHook(() => useHandoff(VERIFY_ADDRESS_KEY));

    expect(result.current).toBe("ada@example.com");
    expect(window.location.href).not.toContain("ada@example.com");
  });

  it("consumes it, so a later visit does not name an address this attempt never used", async () => {
    setHandoff(VERIFY_ADDRESS_KEY, "ada@example.com");
    renderHook(() => useHandoff(VERIFY_ADDRESS_KEY));

    await waitFor(() =>
      expect(window.sessionStorage.getItem(VERIFY_ADDRESS_KEY)).toBeNull(),
    );

    const second = renderHook(() => useHandoff(VERIFY_ADDRESS_KEY));
    expect(second.result.current).toBeNull();
  });

  it("is null when nothing was handed over", () => {
    const { result } = renderHook(() => useHandoff(VERIFY_ADDRESS_KEY));

    expect(result.current).toBeNull();
  });
});
