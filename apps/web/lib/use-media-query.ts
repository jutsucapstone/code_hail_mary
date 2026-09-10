import { useCallback, useSyncExternalStore } from "react";

/**
 * Whether a media query matches, answered synchronously on the first client render.
 *
 * `useSyncExternalStore` rather than state corrected from an effect, for the reason
 * `components/product/kt-scene.tsx` records: a gate that fixes itself one render late
 * has already let what it guards begin — here a WebGL context and a microphone.
 *
 * The server, and a DOM without `matchMedia` (the component tests), answer `false`. No
 * `MediaQueryList` is cached at module scope, so a test that stubs `matchMedia` is read
 * as stubbed rather than as whatever an earlier test left behind.
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (notify: () => void) => {
      if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
        return () => {};
      }
      const list = window.matchMedia(query);
      list.addEventListener("change", notify);
      return () => list.removeEventListener("change", notify);
    },
    [query],
  );
  const getSnapshot = useCallback(
    () =>
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia(query).matches,
    [query],
  );
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}

/** Tailwind's `lg` breakpoint, exactly, so a column and its content cannot disagree. */
export const WIDE_QUERY = "(min-width: 1024px)";

export const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";
