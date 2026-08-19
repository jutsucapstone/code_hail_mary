"use client";

import { useSyncExternalStore } from "react";
import { ArrowRight, X } from "lucide-react";

import { announcement } from "@/lib/content";

const STORAGE_KEY = `jutsu:announcement:${announcement.id}`;
const EVENT = "jutsu:announcement-change";

/**
 * localStorage is a genuine external store, so it is read through
 * `useSyncExternalStore` rather than mirrored into state from an effect.
 * The server snapshot reports "dismissed" so the bar is absent from the SSR
 * markup and can never flash in for someone who already closed it.
 */
const subscribe = (onChange: () => void) => {
  window.addEventListener(EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
};

const isDismissed = () => {
  try {
    return localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    // Private mode / storage blocked — show the bar rather than fail closed.
    return false;
  }
};

export function AnnouncementBar() {
  const dismissed = useSyncExternalStore(subscribe, isDismissed, () => true);

  if (dismissed) return null;

  const dismiss = () => {
    try {
      localStorage.setItem(STORAGE_KEY, "1");
    } catch {
      /* non-fatal */
    }
    window.dispatchEvent(new Event(EVENT));
  };

  return (
    <div className="relative z-50 border-b border-hairline bg-surface/80 backdrop-blur-xl">
      {/* Extra inline-end padding reserves room for the absolutely-positioned
          dismiss button — without it the centred copy wraps under the X on
          narrow viewports. */}
      <div className="mx-auto flex w-full max-w-7xl items-center justify-center gap-3 py-2 pl-6 pr-12 lg:pl-8 lg:pr-14">
        <span className="hidden rounded-full bg-brand/12 px-2 py-0.5 font-mono text-[0.625rem] font-medium uppercase tracking-[0.16em] text-brand sm:inline">
          {announcement.label}
        </span>
        <p className="text-center text-[0.8125rem] text-muted-foreground">
          {announcement.message}{" "}
          <a
            href={announcement.cta.href}
            className="group inline-flex items-center gap-1 rounded font-medium text-foreground underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            {announcement.cta.label}
            <ArrowRight
              aria-hidden="true"
              className="size-3 transition-transform duration-300 group-hover:translate-x-0.5"
            />
          </a>
        </p>
        <button
          type="button"
          onClick={dismiss}
          aria-label="Dismiss announcement"
          className="absolute right-4 inline-flex size-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand lg:right-6"
        >
          <X aria-hidden="true" className="size-3.5" />
        </button>
      </div>
    </div>
  );
}
