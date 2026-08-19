"use client";

import { useEffect } from "react";

/**
 * Makes a refresh deterministic.
 *
 * By default the browser restores the previous scroll offset on reload, which
 * drops the visitor into the middle of a scroll-choreographed page: the
 * manifesto reveal is mid-sweep, and any entrance animation whose element is
 * now *above* the viewport has never intersected, so it sits at its hidden
 * initial state until you happen to scroll back up past it.
 *
 * Taking manual control and starting at the top means every reload plays the
 * page exactly as designed. Deep links still work — an incoming `#hash` is
 * honoured instead of being overridden.
 */
export function ScrollReset() {
  useEffect(() => {
    if (!("scrollRestoration" in history)) return;

    const previous = history.scrollRestoration;
    history.scrollRestoration = "manual";

    if (!window.location.hash) {
      window.scrollTo(0, 0);
    }

    return () => {
      history.scrollRestoration = previous;
    };
  }, []);

  return null;
}
