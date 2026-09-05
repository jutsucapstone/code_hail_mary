"use client";

import { useEffect, useRef } from "react";

import { cn } from "@/lib/utils";

/**
 * A Spline 3D scene, drawn on a transparent canvas.
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
 * egress proxy blocks the CDN — it is an inert transparent block. Decoration degrades
 * to nothing, never to an error a person has to read.
 *
 * `background` is a declared attribute of the element, so "transparent" is the
 * supported way to drop the canvas clear colour rather than a trick; the object is left
 * floating on whatever the page puts behind it.
 */
interface SplineSceneProps {
  /** Scene URL. Self-hosted under /public so no third party serves the content. */
  scene: string;
  className?: string;
  /** Canvas clear colour. Any CSS colour, or "transparent" to show the page through. */
  background?: string;
}

// Pinned, and immutable at this URL. An unpinned "latest" would let a vendor publish
// change how a shipped page renders without a deploy.
const VIEWER_SRC = "https://unpkg.com/@splinetool/viewer@2.0.37/build/spline-viewer.js";

// How long to keep looking for the shadow root before giving up. The element upgrades
// only once the module arrives, which on a cold cache is a network round trip.
const UPGRADE_DEADLINE_MS = 30_000;
const UPGRADE_POLL_MS = 100;

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

/**
 * Hide the viewer's own "Built with Spline" badge.
 *
 * The badge is an `<a id="logo">` the element renders into its (open) shadow root, and
 * it is not exposed as a CSS part, so a stylesheet on the page cannot reach it. A style
 * node injected into the shadow root can — and is used in preference to removing the
 * anchor, because a rule keeps applying if the viewer ever re-renders its own subtree
 * while a removed node would simply come back.
 *
 * The shadow root does not exist until the element upgrades, which is why this polls on
 * a deadline instead of reading it once. Failure is silent by design: if the badge
 * cannot be reached the scene still renders, and a decoration must not throw.
 */
function useHiddenSplineBadge(hostRef: React.RefObject<HTMLElement | null>) {
  useEffect(() => {
    const host = hostRef.current;
    if (host === null) return;

    let timer: number | undefined;
    const startedAt = Date.now();

    const hide = () => {
      const root = host.shadowRoot;
      if (root === null) return false;
      if (root.querySelector("style[data-hide-spline-badge]") !== null) return true;
      const style = document.createElement("style");
      style.dataset.hideSplineBadge = "";
      style.textContent = "#logo { display: none !important; }";
      root.appendChild(style);
      return true;
    };

    const poll = () => {
      if (hide()) return;
      if (Date.now() - startedAt > UPGRADE_DEADLINE_MS) return;
      timer = window.setTimeout(poll, UPGRADE_POLL_MS);
    };
    poll();

    return () => window.clearTimeout(timer);
  }, [hostRef]);
}

export function SplineScene({ scene, className, background = "transparent" }: SplineSceneProps) {
  const viewerRef = useRef<HTMLElement | null>(null);
  useSplineViewer();
  useHiddenSplineBadge(viewerRef);

  return (
    <spline-viewer
      ref={viewerRef}
      url={scene}
      background={background}
      loading-anim-type="none"
      className={cn("block h-full w-full", className)}
    />
  );
}

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace React.JSX {
    interface IntrinsicElements {
      "spline-viewer": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & {
        ref?: React.Ref<HTMLElement | null>;
        url?: string;
        background?: string;
        "loading-anim-type"?: string;
      };
    }
  }
}
