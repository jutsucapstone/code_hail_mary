"use client";

import { useEffect, useRef } from "react";

import { AnnouncementBar } from "@/components/site/announcement-bar";
import { SiteHeader } from "@/components/site/site-header";

/**
 * The fixed top chrome: announcement bar stacked above the header.
 *
 * They have to share one fixed container. When the header pinned itself with
 * `fixed top-0`, an announcement bar rendered before it in the DOM stayed in
 * normal flow and the header simply drew on top of it — logo, nav and bar text
 * all colliding in the same 70px.
 *
 * The real height is published as `--chrome-h` so anchor targets can clear it.
 * It is measured rather than hard-coded because the bar is dismissible and the
 * header changes height at `lg` — a fixed magic number would be wrong in three
 * of the four combinations.
 */
export function SiteChrome() {
  const ref = useRef<HTMLDivElement | null>(null);

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
    const observer = new ResizeObserver(publish);
    observer.observe(el);
    return () => {
      observer.disconnect();
      document.documentElement.style.removeProperty("--chrome-h");
    };
  }, []);

  return (
    <div ref={ref} className="fixed inset-x-0 top-0 z-50">
      <AnnouncementBar />
      <SiteHeader />
    </div>
  );
}
