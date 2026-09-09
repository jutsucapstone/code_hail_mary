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
  /**
   * Fired once the scene has drawn. Anything positioned *against* the rendered
   * object — a badge over a specific part of it — has to wait for this, or it
   * hangs in empty space for the seconds the scene file takes to arrive.
   */
  onReady?: () => void;
}

// Vendored, for the reason the scene itself is: a script served from someone else's
// origin can be republished, withdrawn or compromised and change what a shipped page
// executes with no deploy of ours — and for a *script* that is a far larger claim than
// it is for a scene file. Pinning the URL bounded the version, not the trust.
//
// `next.config.ts` named this as the change that lets `script-src` drop to
// `'self' 'unsafe-inline'`, and it now has. The cost is 3.5MB of deploy weight, paid
// once, against a third party in the execution path of every page that renders a scene.
//
// The bundle lazily imports `./boolean.js`, `./physics.js` and friends for scene
// features this one does not use; those siblings are not vendored, so a scene that
// needed them would 404 here where it previously reached the CDN. That is a deliberate
// bound, not an oversight — vendoring the whole runtime tree is a different decision.
const VIEWER_SRC = "/spline/spline-viewer.js";

// How long to keep looking for the shadow root before giving up. The element upgrades
// only once the module arrives, which on a cold cache is a network round trip.
const UPGRADE_DEADLINE_MS = 30_000;
const UPGRADE_POLL_MS = 100;

/**
 * Where the runtime looks for its WebAssembly. Same directory as everything else.
 */
const WASM_PATH = "/spline";

/**
 * Point the runtime's WASM at this origin instead of Spline's CDN.
 *
 * **This is what was actually wrong with the robot.** The viewer loads
 * `process.wasm` (and friends) from
 * `https://cdn.spline.design/@splinetool/runtime@<v>/build`, an origin `connect-src`
 * has never listed — so the fetch was refused, the scene never finished, and the panel
 * sat on its placeholder. It failed silently and it failed from the day the CSP landed,
 * which is after the scene did: nothing reported it because a blocked decoration looks
 * exactly like a decoration that has not arrived yet.
 *
 * The runtime supports this properly: `wasmPath` is a constructor option on its
 * Application class, and every loader resolves against `this._wasmPath` rather than
 * the constant. What it does NOT have is a way through the custom element, whose
 * factory builds the app with `{renderer}` and nothing else. So the factory is what we
 * wrap — one method, on a version we pin, to pass the option the runtime already
 * documents.
 *
 * Deliberately not the alternatives: widening `connect-src` to a third-party origin
 * spends real security on a decoration and would have to be re-argued at every audit,
 * and rewriting the URL inside the minified bundle would destroy the property that
 * makes vendoring trustworthy — that the bytes are the vendor's, unmodified.
 *
 * Fails open. If a future version renames the method the patch does nothing, the
 * runtime falls back to its CDN, the fetch is refused, and the panel keeps its still —
 * exactly today's behaviour, never a crash. `spline-scene.test.ts` fails if the hook
 * point disappears, so the silence is caught here rather than in production.
 */
export function pointWasmAtThisOrigin(constructor: unknown): boolean {
  const proto = (constructor as { prototype?: Record<string, unknown> } | undefined)?.prototype;
  if (proto === undefined) return false;
  if (proto.__jutsuWasmPathPatched === true) return true;

  const create = proto._createApplication;
  if (typeof create !== "function") return false;

  proto._createApplication = function patched(this: unknown, ...args: unknown[]) {
    const app = (create as (...a: unknown[]) => Record<string, unknown>).apply(this, args);
    // The runtime strips trailing slashes from the option; match it exactly.
    if (app !== null && typeof app === "object") app._wasmPath = WASM_PATH;
    return app;
  };
  proto.__jutsuWasmPathPatched = true;
  return true;
}

function useSplineViewer() {
  useEffect(() => {
    if (document.querySelector("script[data-spline-viewer]")) return;
    const script = document.createElement("script");
    script.type = "module";
    script.src = VIEWER_SRC;
    script.dataset.splineViewer = "";
    document.head.appendChild(script);

    // The element registers when the module evaluates; patch its factory before any
    // instance is constructed. `whenDefined` is the only ordering guarantee available —
    // the script tag's own `load` fires before the module body has necessarily run.
    void customElements
      .whenDefined("spline-viewer")
      .then((constructor) => pointWasmAtThisOrigin(constructor))
      .catch(() => {
        /* No element, no scene, and the caller already renders without one. */
      });
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

/** Resolve once the element says it has drawn. `load-complete` is the viewer's own
 *  event name; `rendered` backs it up, since either means there is something on the
 *  canvas and both are idempotent behind the caller's own state. */
function useSceneReady(hostRef: React.RefObject<HTMLElement | null>, onReady?: () => void) {
  useEffect(() => {
    const host = hostRef.current;
    if (host === null || onReady === undefined) return;
    const handle = () => onReady();
    host.addEventListener("load-complete", handle);
    host.addEventListener("rendered", handle);
    return () => {
      host.removeEventListener("load-complete", handle);
      host.removeEventListener("rendered", handle);
    };
  }, [hostRef, onReady]);
}

export function SplineScene({
  scene,
  className,
  background = "transparent",
  onReady,
}: SplineSceneProps) {
  const viewerRef = useRef<HTMLElement | null>(null);
  useSplineViewer();
  useHiddenSplineBadge(viewerRef);
  useSceneReady(viewerRef, onReady);

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
