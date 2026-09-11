"use client";

import { SplineScene } from "@/components/ui/spline-scene";

export interface SplineRobotProps {
  /** Scene URL. In this app it must be served from this origin — see below. */
  scene: string;
  className?: string;
}

/**
 * A 3D Spline robot and nothing else — no card, background, border, shadow, text or
 * loading mark. It fills whatever box it is given:
 *
 *   <SplineRobot scene="/spline/kt-robot.splinecode" className="h-[600px] w-full" />
 *
 * It follows the pointer anywhere on the page (`events-target="global"`), not only
 * while the pointer is over its own canvas. Until the scene has drawn the box is
 * simply transparent, and the runtime downloads only when this mounts.
 *
 * **Why this is not `@splinetool/react-spline`.** Tried on 2026-09-11 with
 * react-spline 4.1.0 and @splinetool/runtime 2.0.44: `next build` fails with six
 * Turbopack "Module not found" errors, because the runtime references draco decoder
 * files and a boolean WASM module that the package does not ship. The same Spline
 * runtime also ships as the self-contained `<spline-viewer>` bundle, vendored under
 * `public/spline/` and loaded by `SplineScene`, which the bundler never parses.
 *
 * **Scenes are self-hosted.** The CSP's `connect-src` names no Spline origin, so a
 * `prod.spline.design` URL is refused in production. Download the `.splinecode` into
 * `public/spline/` and pass its path. `/spline/kt-robot.splinecode` is byte-for-byte
 * prod.spline.design/kZDDjO5HuC9GJUM2/scene.splinecode.
 */
export function SplineRobot({ scene, className }: SplineRobotProps) {
  return (
    <div className={className}>
      <SplineScene scene={scene} eventsTarget="global" />
    </div>
  );
}
