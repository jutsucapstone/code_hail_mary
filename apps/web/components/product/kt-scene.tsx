"use client";

import { useEffect, useRef, useState } from "react";

import { SplineRobot } from "@/components/ui/spline-robot";
import { REDUCED_MOTION_QUERY, useMediaQuery, WIDE_QUERY } from "@/lib/use-media-query";
import { cn } from "@/lib/utils";

/**
 * The 3D robot beside the KT entry form — the robot and nothing else.
 *
 * No card, no backdrop, no badge and no placeholder mark: the canvas is transparent and
 * the column draws nothing of its own, so until the scene has drawn the column is
 * simply empty, and after it the robot stands on the page in both themes.
 *
 * The scene is self-hosted (`/public/spline`, ~1.3MB). It is byte-for-byte the file
 * published at prod.spline.design/kZDDjO5HuC9GJUM2 — checked, not assumed — kept here
 * because a scene fetched from someone else's bucket can be re-published or withdrawn,
 * changing what a shipped page renders with no deploy of ours, and because
 * `connect-src` names no Spline origin.
 *
 * Three gates stand before anything downloads, because decoration must never tax the
 * person who cannot see it:
 *
 * * it renders at `lg` and up only — below that the page has no second column;
 * * `prefers-reduced-motion` skips it, since ambient motion is the whole point of the
 *   scene; the column then stays empty, which is the honest rendering of "no motion";
 * * the canvas mounts only while the column's *measured* box is non-zero. Given a
 *   zero-size host the runtime allocates a 0×0 surface and fails on every frame, and
 *   window width alone cannot rule that out, since the column can be `display: none`
 *   or mid-layout while the window is wide.
 */

const SCENE = "/spline/kt-robot.splinecode";

export function KtScene({ className }: { className?: string }) {
  const wide = useMediaQuery(WIDE_QUERY);
  const reduced = useMediaQuery(REDUCED_MOTION_QUERY);
  const hostRef = useRef<HTMLDivElement | null>(null);
  // Without ResizeObserver there is no way to measure and so no way to protect —
  // start visible there rather than silently never rendering.
  const [hasSize, setHasSize] = useState(() => typeof ResizeObserver !== "function");

  useEffect(() => {
    if (!wide) return;
    const host = hostRef.current;
    if (host === null || typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver((entries) => {
      const box = entries[entries.length - 1]?.contentRect;
      setHasSize(box !== undefined && box.width >= 1 && box.height >= 1);
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, [wide]);

  // Below `lg` the page gives this no column at all (`hidden lg:block`), so there is
  // nothing to show and nothing to download.
  if (!wide) return null;

  return (
    <div aria-hidden="true" className={cn("relative", className)} ref={hostRef}>
      {!reduced && hasSize ? <SplineRobot scene={SCENE} className="h-full w-full" /> : null}
    </div>
  );
}
