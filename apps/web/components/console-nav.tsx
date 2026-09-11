"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * One navigation vocabulary for both consoles.
 *
 * The admin shell grew this rendering inline; the member shell had no navigation at all.
 * Two shells disagreeing about what "not built yet" looks like is how a reader learns
 * that greyed-out means something different depending on which page they are on.
 *
 * **A pending item is text, never a link.** `ADMIN_SECTIONS` used to claim in its header
 * comment that "a section cannot be linked without also being routed" — that was the
 * intent and not the behaviour, and four of six rendered as doors onto 404s. Listing the
 * section is right, because the shape of the product is worth showing; linking it is not
 * (§4.11). The slice that delivers it is rendered beside the name rather than hidden in
 * a tooltip, so the answer to "when" is on screen.
 *
 * **The vertical sidebar scrolls on its own.** The admin shell is exactly one viewport
 * tall and clips its overflow, so its main column can scroll a table by itself. The
 * sidebar sat in that same clipped row with no scroll area of its own, and on a laptop
 * screen everything past Organisation — the whole of Operations — was cut off with no way
 * to reach it. The list is now its own scroller: the current section is brought into view
 * when it changes, and a soft edge marks where more sections are hidden.
 */

export interface ConsoleNavItem {
  href: string;
  name: string;
  description: string;
  status: "live" | "pending";
  slice: string;
  /** Optional IA grouping. Only read when the caller passes `groups`. */
  group?: string;
}

/** A titled run of sections. `label: null` renders the run with no heading. */
export interface ConsoleNavGroup {
  label: string | null;
  items: readonly ConsoleNavItem[];
}

/** Space kept between a section brought into view and the edge of the list. */
const REVEAL_MARGIN = 12;

function Item({ item, current }: { item: ConsoleNavItem; current: boolean }) {
  if (item.status === "pending") {
    return (
      <li className="shrink-0">
        <div
          // Not `aria-disabled` on a non-interactive element: there is no control
          // here to disable. It is a list entry that says what is coming.
          className="rounded-lg border border-transparent px-3 py-2"
        >
          <span className="flex items-center justify-between gap-2 text-sm text-muted-foreground/70">
            {item.name}
            <span className="font-mono text-[0.625rem] uppercase tracking-[0.14em] text-muted-foreground/60">
              {item.slice}
            </span>
          </span>
          {/* On screen, not a `title` tooltip: touch and keyboard never see one. */}
          <p className="mt-0.5 max-w-56 text-xs leading-relaxed text-muted-foreground/60">
            {item.description} Arrives in {item.slice}.
          </p>
        </div>
      </li>
    );
  }

  return (
    <li className="shrink-0">
      <Link
        href={item.href}
        aria-current={current ? "page" : undefined}
        className={cn(
          "block rounded-lg px-3 py-2 text-sm transition-colors duration-200",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
          current
            ? "border border-brand/40 bg-brand/8 text-foreground"
            : "border border-transparent text-muted-foreground hover:text-foreground",
        )}
      >
        {item.name}
      </Link>
    </li>
  );
}

/**
 * Whether sections are scrolled out of sight above or below the list's visible part.
 *
 * Measured on scroll and on resize, not once: making the window shorter hides the last
 * group and making it taller reveals it, without the list itself changing. `size` is the
 * number of sections, so a list that gains or loses one is measured again too.
 */
function useHiddenEnds(
  scroller: React.RefObject<HTMLDivElement | null>,
  enabled: boolean,
  size: number,
) {
  const [ends, setEnds] = useState({ above: false, below: false });

  useEffect(() => {
    const element = scroller.current;
    if (!enabled || element === null) return;

    const measure = () => {
      // A pixel of slack either way: at a fractional zoom the scroll position never lands
      // exactly on the end, and a fade that never clears reads as "there is more".
      const above = element.scrollTop > 1;
      const below = element.scrollTop + element.clientHeight < element.scrollHeight - 1;
      setEnds((current) =>
        current.above === above && current.below === below ? current : { above, below },
      );
    };

    measure();
    element.addEventListener("scroll", measure, { passive: true });
    const resize = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
    resize?.observe(element);
    return () => {
      element.removeEventListener("scroll", measure);
      resize?.disconnect();
    };
  }, [scroller, enabled, size]);

  return ends;
}

/**
 * A soft edge over sections scrolled out of sight: the sign that the list goes on.
 * Decoration only, so it is hidden from assistive technology and never takes a click.
 * It stops short of the right edge so the scrollbar stays clear to see and grab.
 */
function Fade({ edge, visible }: { edge: "above" | "below"; visible: boolean }) {
  return (
    <div
      aria-hidden="true"
      data-fade={edge}
      data-visible={visible ? "true" : "false"}
      className={cn(
        "pointer-events-none absolute left-0 right-3 hidden h-8 from-background to-transparent transition-opacity duration-200 motion-reduce:transition-none lg:block",
        edge === "above" ? "top-0 bg-gradient-to-b" : "bottom-0 bg-gradient-to-t",
        visible ? "opacity-100" : "opacity-0",
      )}
    />
  );
}

export function ConsoleNav({
  items,
  groups,
  label,
  orientation = "vertical",
}: {
  /** A flat section list. Equivalent to one unlabelled group. */
  items?: readonly ConsoleNavItem[];
  /** Sections under IA headings (§4). Takes precedence over `items`. */
  groups?: readonly ConsoleNavGroup[];
  label: string;
  orientation?: "vertical" | "horizontal";
}) {
  const pathname = usePathname();
  const vertical = orientation === "vertical";
  const scroller = useRef<HTMLDivElement>(null);

  const resolved: readonly ConsoleNavGroup[] =
    groups ?? (items ? [{ label: null, items }] : []);
  const size = resolved.reduce((count, section) => count + section.items.length, 0);
  const ends = useHiddenEnds(scroller, vertical, size);

  // Bring the current section into view whenever it changes. Arriving on System health
  // from a bookmark, or from a link inside another page, used to leave it below the
  // fold with nothing on screen to say where you were.
  //
  // It moves the list's own scroll position and nothing else. `scrollIntoView` would also
  // scroll every scrollable ancestor, the shell's clipped column among them, and push the
  // header out of the viewport. Keyed on the path alone, so a re-render never snaps the
  // list back after somebody has scrolled it themselves.
  useEffect(() => {
    const element = scroller.current;
    const current = element?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!element || !current || typeof element.scrollTo !== "function") return;

    const box = element.getBoundingClientRect();
    const item = current.getBoundingClientRect();
    const down = item.bottom > box.bottom ? item.bottom - box.bottom + REVEAL_MARGIN : 0;
    const up = item.top < box.top ? box.top - item.top + REVEAL_MARGIN : 0;
    // The same sideways, for the horizontal strip the sidebar becomes below `lg`.
    const right = item.right > box.right ? item.right - box.right + REVEAL_MARGIN : 0;
    const left = item.left < box.left ? box.left - item.left + REVEAL_MARGIN : 0;

    if (down || up || right || left) {
      element.scrollTo({
        top: element.scrollTop + down - up,
        left: element.scrollLeft + right - left,
      });
    }
  }, [pathname]);

  // One scroller, not one per group.
  //
  // Below `lg` the sidebar collapses into a single horizontal strip. Putting
  // `overflow-x-auto` on each group's `<ul>` — which is what it looked like it wanted —
  // gives every group its own scrollbar sitting side by side, so the reader gets two or
  // three little independently-scrolling rails instead of one list. The scroller belongs
  // to the container that holds all of them; the lists inside just lay out. From `lg` up
  // the same element scrolls vertically instead, inside the height the shell gives it.
  const listClass = vertical ? "flex gap-1 lg:flex-col" : "flex flex-wrap gap-1";

  return (
    <nav
      aria-label={label}
      className={vertical ? "relative lg:flex lg:w-56 lg:shrink-0 lg:flex-col" : ""}
    >
      <div
        ref={scroller}
        className={cn(
          "flex gap-1",
          vertical
            ? // The small padding, taken back by the negative margin, is room for a focus
              // outline: a scroller clips its content, and a 2px ring with a 2px offset on
              // the first or last link would otherwise be cut in half.
              "overflow-x-auto lg:-mx-1 lg:min-h-0 lg:flex-1 lg:flex-col lg:gap-6 lg:overflow-x-hidden lg:overflow-y-auto lg:overscroll-contain lg:px-1 lg:py-1 lg:[scrollbar-gutter:stable] lg:[scrollbar-width:thin]"
            : "flex-wrap",
        )}
      >
        {resolved.map((section, index) => (
          <div
            key={section.label ?? `ungrouped-${index}`}
            className={vertical ? "shrink-0 lg:shrink" : "contents"}
          >
            {/* The IA heading, in the signature micro-label: mono, uppercase, tracked.
                Hidden on the horizontal strip, where a heading between wrapped rows reads
                as an orphan rather than as structure — the inline console has one group
                and nothing to separate. */}
            {section.label && vertical ? (
              <h2 className="mb-1.5 hidden px-3 font-mono text-[0.625rem] uppercase tracking-[0.14em] text-muted-foreground/60 lg:block">
                {section.label}
              </h2>
            ) : null}
            <ul className={listClass}>
              {section.items.map((item) => (
                <Item key={item.href} item={item} current={pathname === item.href} />
              ))}
            </ul>
          </div>
        ))}
      </div>
      {vertical ? (
        <>
          <Fade edge="above" visible={ends.above} />
          <Fade edge="below" visible={ends.below} />
        </>
      ) : null}
    </nav>
  );
}
