import { ChevronRight } from "lucide-react";

import { TextRevealByWord } from "@/components/ui/text-reveal";
import { manifesto } from "@/lib/content";

/**
 * The cold open. Nothing but the thesis, revealed word by word as you scroll.
 *
 * Layout note: nothing in this subtree may set `overflow: hidden` (or `auto`) —
 * that turns the ancestor into a scroll container, which both breaks the sticky
 * pin and detaches `useScroll` from the window. `overflow-clip` is safe and is
 * what the decorative glow uses.
 */
export function OpeningStatement() {
  return (
    <section id="manifesto" aria-label="Why JUTSU exists" className="relative isolate">
      <div aria-hidden="true" className="hairline-grid radial-fade absolute inset-0" />
      <div aria-hidden="true" className="absolute inset-0 overflow-clip">
        <div className="glow-warm absolute -top-32 left-1/2 h-[38rem] w-[70rem] max-w-[140vw] -translate-x-1/2 rounded-[50%] blur-[120px]" />
      </div>

      <TextRevealByWord text={manifesto.line} className="h-[220vh]" />

      {/* Pinned frame: sits above the reveal, never intercepts pointer events. */}
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 z-10">
        <div className="sticky top-0 h-screen">
          <div className="mx-auto flex h-full max-w-5xl flex-col justify-between px-4 py-20 sm:px-6 sm:py-24 lg:px-8">
            <p className="eyebrow flex items-center gap-3 text-brand">
              <span className="h-1 w-1 rounded-full bg-brand" />
              {manifesto.eyebrow}
            </p>
            <p className="eyebrow flex items-center gap-2 text-muted-foreground/80">
              {manifesto.footnote}
              <ChevronRight className="size-3 rotate-90 animate-bounce" />
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
