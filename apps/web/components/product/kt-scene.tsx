"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useReducedMotion } from "framer-motion";

/**
 * The decorative 3D scene beside the KT entry form.
 *
 * Delivery is the interesting decision. The scene file itself is ours and small, so
 * it is self-hosted (/public, 31KB). The renderer is Spline's official `spline-viewer`
 * web component, loaded as a module script pinned to the scene's exact authoring
 * version — the npm distribution of that same runtime is unpackagable (its 2.x line
 * depends on `@splinetool/animation-core`, which Spline never published, and the
 * builds reference decoder files absent from the tarball, which Turbopack rightly
 * refuses at build time). A pinned unpkg URL is immutable, and a runtime that fails
 * to arrive degrades this panel to an empty framed surface — decoration falls back to
 * nothing, never to an error a person has to read.
 *
 * Three gates before a byte of WebGL downloads, because decoration must never tax the
 * person who cannot see it: the script is injected only when this component decides to
 * render; it renders only at `lg` and up (on a phone the column does not exist); and
 * `prefers-reduced-motion` skips it entirely, since the scene's whole point is ambient
 * motion.
 *
 * A fourth gate exists because of what the runtime does with a zero-size host: it
 * boots WebGPU anyway and then fails to allocate a 0×0 swapchain texture on every
 * frame, for ever — hundreds of `GPUValidationError`s a minute that Next's dev overlay
 * dutifully counts as issues. Window width alone cannot rule that out (the panel can
 * be `display: none` or mid-layout while the window is wide), so the viewer element
 * itself mounts only while the panel's *measured* box is non-zero, and unmounts the
 * moment it collapses.
 */

const VIEWER_SRC = "https://unpkg.com/@splinetool/viewer@2.0.21/build/spline-viewer.js";

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
  const active = wide && !shouldReduceMotion;
  const hostRef = useRef<HTMLDivElement | null>(null);
  // Without ResizeObserver there is no way to measure and so no way to protect —
  // start visible there (the plain mount) rather than silently never rendering.
  const [hasSize, setHasSize] = useState(() => typeof ResizeObserver !== "function");

  useEffect(() => {
    if (!active) return;
    if (document.querySelector("script[data-spline-viewer]")) return;
    const script = document.createElement("script");
    script.type = "module";
    script.src = VIEWER_SRC;
    script.dataset.splineViewer = "";
    document.head.appendChild(script);
    // Deliberately never removed: a module script cannot be un-executed, and the
    // custom element stays registered for the life of the page either way.
  }, [active]);

  useEffect(() => {
    if (!active) return;
    const host = hostRef.current;
    if (host === null) return;
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver((entries) => {
      const box = entries[entries.length - 1]?.contentRect;
      setHasSize(box !== undefined && box.width >= 1 && box.height >= 1);
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, [active]);

  if (!active) return null;

  return (
    <div aria-hidden="true" className={className} ref={hostRef}>
      {/* The custom element upgrades in place once the module registers it; until
          then it is an inert block and the panel's own surface shows. The scene keeps
          its own dark rendering deliberately: its glass is lit against the backdrop
          the artist gave it, and removing that (canvas override or hiding the
          backdrop mesh) collapses the material into an unlit blob — verified live.
          The watermark is off in the scene file itself, via the same
          publish.settings.web.logo flag the Spline editor writes on export. */}
      {hasSize ? (
        <spline-viewer
          url="/spline/kt-orb.splinecode"
          loading-anim-type="none"
          style={{ width: "100%", height: "100%", display: "block" }}
        />
      ) : null}
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
