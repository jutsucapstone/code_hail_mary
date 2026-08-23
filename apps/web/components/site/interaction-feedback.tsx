"use client";

import { useEffect } from "react";

import { startFeedback } from "@/lib/feedback";

/**
 * Mounts the delegated press listeners once, for the whole app.
 *
 * Renders nothing. It lives in the root layout so marketing, pilot and admin all behave
 * the same way — a control that ticks on the landing page and is silent inside the
 * product is worse than one that never ticks at all.
 *
 * Nothing is created until someone presses something, and nothing is created ever unless
 * the visitor has turned sound on, so the cost of this on a page view that never
 * interacts is two event listeners.
 */
export function InteractionFeedback() {
  useEffect(() => startFeedback(), []);
  return null;
}
