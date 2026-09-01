"use client";

import { useEffect, useRef, useState } from "react";

import { AnnouncementBar } from "@/components/site/announcement-bar";
import { SiteHeader } from "@/components/site/site-header";
import { cn } from "@/lib/utils";

/**
 * The fixed top chrome: announcement bar stacked above the header.
 *
 * They have to share one fixed container. When the header pinned itself with
 * `fixed top-0`, an announcement bar rendered before it in the DOM stayed in
 * normal flow and the header simply drew on top of it — logo, nav and bar text
 * all colliding in the same 70px.
 *
 * The real height is published as `--chrome-h` so anchor targets and the hero can
 * clear it. It is measured rather than hard-coded because the bar is dismissible
 * and the header changes height at `lg` — a fixed magic number would be wrong in
 * three of the four combinations.
 */
export function SiteChrome() {
  const ref = useRef<HTMLDivElement | null>(null);
  const [condensed, setCondensed] = useState(false);

  // Past the hero's first breath, the announcement yields its row to content.
  // The grid-rows trick animates an unknown height; the ResizeObserver below
  // sees the collapse and republishes --chrome-h, so anchors stay correct in
  // both states without a second magic number.
  useEffect(() => {
    const onScroll = () => setCondensed(window.scrollY > 96);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const publish = () => {
      document.documentElement.style.setProperty(
        "--chrome-h",
        `${Math.round(el.getBoundingClientRect().height)}px`,
      );
    };

    publish();

    const resize = new ResizeObserver(publish);
    resize.observe(el);

    // A ResizeObserver alone published the wrong number, every load, on mobile.
    //
    // AnnouncementBar reads its dismissed flag through `useSyncExternalStore`, whose
    // server snapshot reports "dismissed" so the bar is absent from SSR markup and
    // cannot flash in for someone who already closed it. That means it is still absent
    // during the hydration commit — which is when this effect runs and takes its first
    // measurement. The bar arrives in the following render, and that particular
    // transition did not produce a resize notification: measured 121px of real chrome
    // sitting behind a published 65px, stable for as long as the page was open, so the
    // hero and every anchor cleared 56px too little.
    //
    // Watching the subtree catches it, because the cause is a child appearing rather
    // than the box being resized. Both observers are kept: this one sees the bar mount
    // and unmount, the ResizeObserver sees reflow at a breakpoint or on a text resize.
    const mutate = new MutationObserver(publish);
    mutate.observe(el, { childList: true, subtree: true });

    return () => {
      resize.disconnect();
      mutate.disconnect();
      document.documentElement.style.removeProperty("--chrome-h");
    };
  }, []);

  return (
    <div ref={ref} className="fixed inset-x-0 top-0 z-50">
      <div
        className={cn(
          "grid transition-[grid-template-rows] duration-300 ease-out",
          condensed ? "grid-rows-[0fr]" : "grid-rows-[1fr]",
        )}
      >
        <div className="overflow-hidden">
          <AnnouncementBar />
        </div>
      </div>
      <SiteHeader />
    </div>
  );
}
