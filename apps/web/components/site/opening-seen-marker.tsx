"use client";

import { useEffect, useRef } from "react";

import { OPENING_SEEN_KEY } from "@/lib/opening";

/**
 * Records that the visitor has scrolled past the cold open.
 *
 * Deliberately not "mark seen on mount". Someone who lands, reads two words and reloads
 * has not seen it, and spending the one showing on a page view they never engaged with is
 * the wrong side to err on. The flag is written only once this sentinel — which sits at
 * the very bottom of the section — has passed above the viewport.
 *
 * **A position check, not an IntersectionObserver.** The obvious implementation observes
 * the sentinel and marks it seen when it intersects, and that misses every fast scroll:
 * IntersectionObserver fires on state *transitions*, so jumping from the top of the page
 * to below the section — an End keypress, a scrollbar drag, a fling on a trackpad — goes
 * from "not intersecting, below" straight to "not intersecting, above" without ever
 * firing. Found by scrolling instantly past it in a test and watching the flag stay unset.
 *
 * Reading one rect inside a rAF, from a passive listener that detaches the moment it
 * succeeds, costs less than the observer it replaces and cannot be skipped.
 *
 * It never removes the section from the page it is on. Hiding 220vh out from under
 * someone mid-scroll would throw them down the document; the flag takes effect on the
 * next visit, where the inline script applies it before anything paints.
 */
export function OpeningSeenMarker() {
  const sentinel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = sentinel.current;
    if (!element) return;

    const read = (): string | null => {
      try {
        return localStorage.getItem(OPENING_SEEN_KEY);
      } catch {
        // Storage unavailable — private mode, blocked cookies. The cold open then shows
        // every time, which is the harmless direction to fail.
        return "1";
      }
    };

    if (read() === "1") return;

    let frame = 0;
    let done = false;

    const check = () => {
      frame = 0;
      if (done) return;
      // `top <= 0` means the end of the section has passed the top of the viewport, so
      // the reveal has run its course whatever route the scroll took to get there.
      if (element.getBoundingClientRect().top > 0) return;

      done = true;
      try {
        localStorage.setItem(OPENING_SEEN_KEY, "1");
      } catch {
        // Not being able to remember is not worth an error.
      }
      window.removeEventListener("scroll", onScroll);
    };

    const onScroll = () => {
      // One rect per frame at most: the listener runs on every scroll event, and reading
      // layout in each of them is the classic way to make a smooth page stutter.
      if (frame === 0) frame = requestAnimationFrame(check);
    };

    // A tall screen may already show the end of the section without any scrolling.
    check();
    window.addEventListener("scroll", onScroll, { passive: true });

    return () => {
      window.removeEventListener("scroll", onScroll);
      if (frame !== 0) cancelAnimationFrame(frame);
    };
  }, []);

  return <div ref={sentinel} aria-hidden="true" className="h-px w-full" />;
}
