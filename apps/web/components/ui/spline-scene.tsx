"use client";

import { useEffect, useRef } from "react";

import { cn } from "@/lib/utils";

/**
 * A Spline 3D scene, drawn on a transparent canvas.
 *
 * The obvious implementation — `lazy(() => import("@splinetool/react-spline"))` — does
 * not build, and the reason is a defect in the vendor's package rather than anything
 * here. `@splinetool/runtime` references its draco decoders through
 * `new URL("../libs/draco/…", import.meta.url)` and ships no `libs/` directory at all.
 * Turbopack resolves those statically and fails the build with "Module not found",
 * whether or not a given scene uses compressed meshes. Found on 2.0.37 and found again
 * on 2026-09-11 with 2.0.44 and react-spline 4.1.0: six errors, the five decoder files
 * plus `boolean_wasm_bg.wasm`. Making it work would mean patching files sourced from
 * elsewhere into `node_modules` — more supply-chain surface, for a decoration, than
 * the thing it buys.
 *
 * So this renders Spline's own `spline-viewer` custom element instead, from a vendored
 * copy (`VIEWER_SRC` below). That bundle is self-contained: the browser loads one
 * script and the bundler never parses it, so the packaging defect cannot reach our
 * build. The element upgrades in place once the module registers it; until then it is
 * an inert transparent block. Decoration degrades to nothing, never to an error a
 * person has to read.
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
   * Where the scene listens for the pointer — the viewer's own `events-target`.
   * `"global"` follows it across the whole page, so a figure that looks at the cursor
   * keeps doing so while the cursor is on the form beside it rather than only while it
   * is over the canvas.
   */
  eventsTarget?: "local" | "global";
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
 * The viewer loads `process.wasm` (and friends) from
 * `https://cdn.spline.design/@splinetool/runtime@<v>/build`, an origin `connect-src`
 * has never listed — so the fetch was refused, the scene never finished, and the panel
 * stayed empty. It failed silently and it failed from the day the CSP landed, which is
 * after the scene did: nothing reported it because a blocked decoration looks exactly
 * like a decoration that has not arrived yet.
 *
 * That was half of it. The other half is that the module, once fetched, must be
 * COMPILED, which a CSP without `'wasm-unsafe-eval'` refuses — see `script-src` in
 * `next.config.ts`. Development's `'unsafe-eval'` allows it, which is why this looked
 * fixed everywhere except production.
 *
 * The runtime supports the path properly: `wasmPath` is a constructor option on its
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
 * runtime falls back to its CDN, the fetch is refused, and the column stays empty —
 * never a crash. `spline-scene.test.ts` fails if the hook point disappears, so the
 * silence is caught here rather than in production.
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

/**
 * Stop the scene when the component goes.
 *
 * The viewer's own `disconnectedCallback` stops watching the viewport and nothing
 * else: removed from the page, its renderer kept drawing into a 0×0 canvas on every
 * frame, for good — measured at ~280 WebGPU validation errors a second after the KT
 * column unmounted, which it does on every in-app navigation away from /handover and
 * on any resize below `lg`. `unload()` is the viewer's own teardown (it disposes the
 * Spline application), and it is what this calls.
 *
 * Two details carry the weight:
 *
 * * **Only once the element is really gone.** Passive-effect clean-ups run after React
 *   has removed the node, so a detached element is a real unmount; a connected one is
 *   StrictMode re-running effects in development, where unloading would tear down a
 *   scene that is still on screen.
 * * **A load that finishes after the unmount is unloaded too.** `unload()` does
 *   nothing until the scene has loaded, so a column that unmounts mid-load would
 *   otherwise finish loading into a detached element and leak exactly as before. The
 *   late call waits a microtask so it lands after the viewer has marked itself loaded.
 */
function useUnloadOnUnmount(hostRef: React.RefObject<HTMLElement | null>) {
  useEffect(() => {
    const host = hostRef.current as (HTMLElement & { unload?: () => void }) | null;
    if (host === null) return;
    return () => {
      if (host.isConnected) return;
      host.unload?.();
      host.addEventListener("load-complete", () => queueMicrotask(() => host.unload?.()), {
        once: true,
      });
    };
  }, [hostRef]);
}

export function SplineScene({
  scene,
  className,
  background = "transparent",
  eventsTarget,
  onReady,
}: SplineSceneProps) {
  const viewerRef = useRef<HTMLElement | null>(null);
  useSplineViewer();
  useHiddenSplineBadge(viewerRef);
  useSceneReady(viewerRef, onReady);
  useUnloadOnUnmount(viewerRef);

  return (
    <spline-viewer
      ref={viewerRef}
      url={scene}
      background={background}
      events-target={eventsTarget}
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
        "events-target"?: string;
        "loading-anim-type"?: string;
      };
    }
  }
}
