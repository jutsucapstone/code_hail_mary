"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useReducedMotion } from "framer-motion";

import { Logo } from "@/components/site/logo";
import { SplineScene } from "@/components/ui/spline-scene";
import { cn } from "@/lib/utils";

/**
 * The decorative 3D figure beside the KT entry form.
 *
 * It is the object and nothing else: no card, no surface, no backdrop. The canvas is
 * transparent and the panel draws no background of its own, so the robot sits directly
 * on the page in both themes and there is no dark rectangle to keep in step with the
 * palette.
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
 * proxy — if unpkg is unreachable the scene never draws. It degrades to empty space,
 * and nothing else on the page depends on it.
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

/**
 * Where the JUTSU mark sits on the robot's chest, as a fraction of the panel.
 *
 * Measured against the rendered scene rather than guessed, and expressed in
 * percentages so it holds at any panel size. The scene animates its arms and head but
 * keeps the torso on the spot, which is the only reason a flat overlay can pass for a
 * badge on the chest — if the figure ever starts walking, this stops being a
 * positioning problem and becomes a texture that belongs in the scene itself.
 */
const CHEST = "left-[50%] top-[48%] w-9";

/**
 * The JUTSU mark, lit from inside the robot's chest plate.
 *
 * `mix-blend-screen` is what stops this reading as a sticker. Screen leaves black
 * untouched and only ever adds light, so the mark's dark lobe dissolves into the torso
 * while its green lobe brightens whatever the renderer already put there — including
 * the moving specular highlight on the chest. The emblem therefore takes the surface's
 * own shading instead of sitting on top of it flatly, which is the whole difference
 * between an applied decal and something that belongs to the model.
 *
 * The halo underneath is the backlight. It is a plain (non-blended) glow so there is
 * still a soft pool of green on the plate when the chest is in shadow and the screened
 * mark alone would nearly vanish.
 *
 * Both are sized from the same `w-9`, and the panel is a fixed 496×512 at every desktop
 * width (the container caps it), so the mark keeps its proportion to the figure without
 * needing to be measured against the viewport.
 */
function ChestEmblem({ visible }: { visible: boolean }) {
  return (
    <div
      className={cn(
        "pointer-events-none absolute -translate-x-1/2 -translate-y-1/2",
        CHEST,
        "transition-opacity duration-700",
        visible ? "opacity-100" : "opacity-0",
      )}
    >
      <span
        className="absolute -inset-[35%] rounded-full blur-[7px]"
        style={{
          background: "radial-gradient(circle, rgba(122,193,66,0.42), transparent 70%)",
        }}
      />
      <Logo className="relative h-auto w-full mix-blend-screen" />
    </div>
  );
}

export function KtScene({ className }: { className?: string }) {
  const wide = useSyncExternalStore(subscribeWide, getWideSnapshot, getWideServerSnapshot);
  const shouldReduceMotion = useReducedMotion();
  const [sceneReady, setSceneReady] = useState(false);
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
    <div aria-hidden="true" className={cn("isolate", className)} ref={hostRef}>
      {hasSize ? (
        <>
          <SplineScene scene={SCENE} onReady={() => setSceneReady(true)} />
          <ChestEmblem visible={sceneReady} />
        </>
      ) : null}
    </div>
  );
}
