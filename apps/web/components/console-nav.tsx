"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

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

function Item({ item, current }: { item: ConsoleNavItem; current: boolean }) {
  if (item.status === "pending") {
    return (
      <li className="shrink-0">
        <span
          // Not `aria-disabled` on a non-interactive element: there is no control
          // here to disable. It is a list entry that says what is coming.
          className="flex items-center justify-between gap-2 rounded-lg border border-transparent px-3 py-2 text-sm text-muted-foreground/55"
          title={`${item.description} Arrives in ${item.slice}.`}
        >
          {item.name}
          <span className="font-mono text-[0.625rem] uppercase tracking-[0.14em] text-muted-foreground/45">
            {item.slice}
          </span>
        </span>
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

  const resolved: readonly ConsoleNavGroup[] =
    groups ?? (items ? [{ label: null, items }] : []);

  // One scroller, not one per group.
  //
  // Below `lg` the sidebar collapses into a single horizontal strip. Putting
  // `overflow-x-auto` on each group's `<ul>` — which is what it looked like it wanted —
  // gives every group its own scrollbar sitting side by side, so the reader gets two or
  // three little independently-scrolling rails instead of one list. The scroller belongs
  // to the container that holds all of them; the lists inside just lay out.
  const listClass = vertical ? "flex gap-1 lg:flex-col" : "flex flex-wrap gap-1";

  return (
    <nav aria-label={label} className={vertical ? "lg:w-56 lg:shrink-0" : ""}>
      <div
        className={cn(
          "flex gap-1",
          vertical
            ? "overflow-x-auto lg:flex-col lg:gap-6 lg:overflow-visible"
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
    </nav>
  );
}
