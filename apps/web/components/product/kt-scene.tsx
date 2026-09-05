"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useReducedMotion } from "framer-motion";

import { SplineScene } from "@/components/ui/spline-scene";
import { Spotlight } from "@/components/ui/spotlight";
import { cn } from "@/lib/utils";

/**
 * The decorative 3D stage beside the KT entry form.
 *
 * The scene *content* is self-hosted (`/public/spline`, ~1.3MB) rather than streamed
 * from Spline's CDN. That costs deploy weight — the same objection that keeps the
 * full-res logo out of `public/` — and buys something an image never needs: a scene
 * fetched from someone else's bucket can be re-published or withdrawn, changing what a
 * shipped page renders with no deploy of ours. The file carries no external asset
 * references, so hosting it here is complete rather than partial.
 *
 * The *viewer* is still a pinned third-party script (see `spline-scene.tsx` for why the
 * npm package cannot be used), so this is not a claim that the panel survives an egress
 * proxy — if unpkg is unreachable the scene never draws. It degrades to the lit empty
 * stage, which is a designed state rather than a broken one, and nothing else on the
 * page depends on it.
 *
 * Three gates stand before any of that downloads, because decoration must never tax the
 * person who cannot see it:
 *
 * * it renders at `lg` and up only — below that the page has no second column, and the
 *   query matches Tailwind's breakpoint exactly so the two cannot drift;
 * * `prefers-reduced-motion` skips it entirely, since ambient motion is the whole point
 *   of the scene and there is nothing left worth downloading once it is unwelcome;
 * * the viewer mounts only while the panel's *measured* box is non-zero. Given a
 *   zero-size host the runtime boots WebGPU anyway and then fails to allocate a 0×0
 *   swapchain texture on every frame, for ever — hundreds of `GPUValidationError`s a
 *   minute. Window width alone cannot rule that out, since the panel can be
 *   `display: none` or mid-layout while the window is wide.
 *
 * The stage is deliberately dark in both themes. It is a lit surface for a 3D object
 * rather than a themed panel, it carries no text, and it is `aria-hidden` — so there is
 * no contrast pair to keep, and the light theme gets the same designed rendering.
 */

const SCENE = "/spline/kt-robot.splinecode";

const WIDE_QUERY = "(min-width: 1024px)";

let wideMq: MediaQueryList | null = null;
const getWideMq = () => (wideMq ??= window.matchMedia(WIDE_QUERY));

// matchMedia is absent in the component test DOM (and in the odd embedder); a scene
// that cannot ask how wide the screen is answers "not wide enough" and stays away.
const canMatch = () => typeof window.matchMedia === "function";

const subscribeWide = (notify: () => void) => {
  if (!canMatch()) return () => {};
  const mq = getWideMq();
  mq.addEventListener("change", notify);
  return () => mq.removeEventListener("change", notify);
};
const getWideSnapshot = () => canMatch() && getWideMq().matches;
const getWideServerSnapshot = () => false;

export function KtScene({ className }: { className?: string }) {
  const wide = useSyncExternalStore(subscribeWide, getWideSnapshot, getWideServerSnapshot);
  const shouldReduceMotion = useReducedMotion();
  const hostRef = useRef<HTMLDivElement | null>(null);
  // Without ResizeObserver there is no way to measure and so no way to protect —
  // start visible there rather than silently never rendering.
  const [hasSize, setHasSize] = useState(() => typeof ResizeObserver !== "function");

  const active = wide && !shouldReduceMotion;

  useEffect(() => {
    if (!active) return;
    const host = hostRef.current;
    if (host === null || typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver((entries) => {
      const box = entries[entries.length - 1]?.contentRect;
      setHasSize(box !== undefined && box.width >= 1 && box.height >= 1);
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, [active]);

  if (!active) return null;

  return (
    <div aria-hidden="true" className={cn("isolate bg-black/[0.96]", className)} ref={hostRef}>
      <Spotlight className="-top-24 left-8" size={360} />
      {hasSize ? <SplineScene scene={SCENE} /> : null}
    </div>
  );
}
