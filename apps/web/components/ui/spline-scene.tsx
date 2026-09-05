"use client";

import { useEffect } from "react";

import { cn } from "@/lib/utils";

/**
 * A Spline 3D scene.
 *
 * The obvious implementation — `lazy(() => import("@splinetool/react-spline"))` — does
 * not build, and the reason is a defect in the vendor's package rather than anything
 * here. `@splinetool/runtime` (2.0.37, and every version tried) references five draco
 * decoder assets through `new URL("../libs/draco/…", import.meta.url)` and ships no
 * `libs/` directory at all. Turbopack resolves those statically and fails the build
 * with "Module not found", whether or not a given scene uses compressed meshes. Making
 * it work would mean patching decoder binaries sourced from elsewhere into
 * `node_modules` — more supply-chain surface, for a decoration, than the thing it buys.
 *
 * So this renders Spline's own `spline-viewer` custom element instead, from a version-
 * pinned URL. That bundle is self-contained: the browser loads one script and the
 * bundler never parses it, so the packaging defect cannot reach our build. The element
 * upgrades in place once the module registers it; until then — and for ever, if an
 * egress proxy blocks the CDN — it is an inert block and the stage behind it shows.
 * Decoration degrades to less decoration, never to an error a person has to read.
 *
 * The stage is painted behind the scene rather than swapped out on a load event. There
 * is no state to get stuck in that way: the viewer's canvas is transparent until it has
 * something to draw, so the lit surface simply shows through and is covered when the
 * scene arrives.
 */
interface SplineSceneProps {
  /** Scene URL. Self-hosted under /public so no third party serves the content. */
  scene: string;
  className?: string;
}

// Pinned, and immutable at this URL. An unpinned "latest" would let a vendor publish
// change how a shipped page renders without a deploy.
const VIEWER_SRC = "https://unpkg.com/@splinetool/viewer@2.0.37/build/spline-viewer.js";

function useSplineViewer() {
  useEffect(() => {
    if (document.querySelector("script[data-spline-viewer]")) return;
    const script = document.createElement("script");
    script.type = "module";
    script.src = VIEWER_SRC;
    script.dataset.splineViewer = "";
    document.head.appendChild(script);
    // Deliberately never removed: a module script cannot be un-executed, and the custom
    // element stays registered for the life of the page either way.
  }, []);
}

export function SplineScene({ scene, className }: SplineSceneProps) {
  useSplineViewer();

  return (
    <div className={cn("relative h-full w-full", className)}>
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-[radial-gradient(circle_at_50%_45%,rgba(255,255,255,0.08),transparent_65%)]"
      />
      <spline-viewer
        url={scene}
        loading-anim-type="none"
        style={{ width: "100%", height: "100%", display: "block" }}
      />
    </div>
  );
}

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace React.JSX {
    interface IntrinsicElements {
      "spline-viewer": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & {
        url?: string;
        "loading-anim-type"?: string;
      };
    }
  }
}
