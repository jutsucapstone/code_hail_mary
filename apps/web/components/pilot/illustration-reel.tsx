"use client";

import Image from "next/image";
import { useEffect, useState } from "react";
import { useReducedMotion } from "framer-motion";

import { cn } from "@/lib/utils";

/**
 * A slow cross-fade through the pilot illustrations.
 *
 * Elegance here comes from restraint, not from effects. One thing moves at a time, the
 * transition is long enough to read as a dissolve rather than a cut, and the easing is the
 * same `[0.22, 1, 0.36, 1]` curve the rest of the site uses — so it feels like the same
 * product rather than a widget dropped into it.
 *
 * **Both frames stay mounted and the outgoing one is only faded.** Swapping the `src` of a
 * single `<img>` would show a blank frame while the next file decodes, which reads as a
 * flicker on a slow connection and is the usual reason a "smooth" carousel is not.
 *
 * **It never starts under `prefers-reduced-motion`.** A cross-fading image is exactly the
 * kind of ambient movement that setting exists to stop, so the component renders the first
 * illustration statically instead — the CSS floor in globals.css only shortens durations,
 * it cannot stop a timer.
 *
 * The reel is decorative: the surrounding panel carries the meaning, so the whole thing is
 * `aria-hidden` and the images have empty alt text. Announcing "illustration 3 of 5" to a
 * screen reader would be noise, not information.
 */

export interface Illustration {
  src: string;
  /** Intrinsic size, so Next can reserve the box and avoid layout shift. */
  width: number;
  height: number;
}

/** How long each illustration is held, and how long the dissolve takes. */
const HOLD_MS = 4200;
const FADE_MS = 1100;

export function IllustrationReel({
  illustrations,
  className,
  sizes,
}: {
  illustrations: readonly Illustration[];
  className?: string;
  /**
   * The rendered width, for the browser to pick a srcset candidate against.
   *
   * Without it Next falls back to the declared intrinsic width and the browser can settle
   * on a candidate far smaller than the box — measured here at 113px inside a 304px
   * stage, which is a visibly soft image for no reason.
   */
  sizes?: string;
}) {
  const shouldReduceMotion = useReducedMotion();
  const [index, setIndex] = useState(0);

  useEffect(() => {
    if (shouldReduceMotion || illustrations.length < 2) return;

    const timer = window.setInterval(
      () => setIndex((current) => (current + 1) % illustrations.length),
      HOLD_MS,
    );
    return () => window.clearInterval(timer);
  }, [shouldReduceMotion, illustrations.length]);

  if (illustrations.length === 0) return null;

  return (
    // A FIXED SQUARE box, not one sized from the first illustration.
    //
    // The five artworks have genuinely different aspect ratios — 1.10, 1.08, 0.93, 0.82,
    // 0.98 — so taking the container's shape from any one of them makes the others
    // letterbox against it, and the reel appears to change size on every dissolve. That
    // reads as a jolt, which is precisely what a cross-fade is meant to avoid.
    //
    // A square box with `object-contain` gives every frame the same centred stage: each
    // is scaled to fit and nothing reflows. The tall one simply uses more of the height.
    <div
      aria-hidden="true"
      className={cn("relative isolate aspect-square w-full", className)}
    >
      {illustrations.map((illustration, i) => {
        const active = i === index;
        return (
          <Image
            key={illustration.src}
            src={illustration.src}
            alt=""
            width={illustration.width}
            height={illustration.height}
            // The first is eager so the panel is never briefly empty on load; the rest
            // load in the background, well before their turn comes round.
            priority={i === 0}
            sizes={sizes}
            // Served as-is, deliberately.
            //
            // The optimizer never upscales, so for source art this small the best it can
            // do is match the original — and measured, it did worse: it picked variants at
            // 0.62x intrinsic, which is a visibly soft image inside a 240px stage. These
            // are 25-48KB flat-colour PNGs with alpha; re-encoding them saves nothing
            // worth the loss of line quality.
            unoptimized
            className={cn(
              "absolute inset-0 h-full w-full object-contain",
              // A whisper of scale alongside the fade. Enough to feel alive, small enough
              // that nobody consciously notices it — a larger move would draw the eye away
              // from the two choices this page exists to present.
              "transition-[opacity,transform] will-change-[opacity,transform]",
              active ? "opacity-100 scale-100" : "opacity-0 scale-[1.015]",
              "motion-reduce:transition-none",
            )}
            style={{
              transitionDuration: `${FADE_MS}ms`,
              transitionTimingFunction: "cubic-bezier(0.22, 1, 0.36, 1)",
            }}
          />
        );
      })}
    </div>
  );
}
