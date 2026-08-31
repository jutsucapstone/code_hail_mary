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
}

export function ConsoleNav({
  items,
  label,
  orientation = "vertical",
}: {
  items: readonly ConsoleNavItem[];
  label: string;
  orientation?: "vertical" | "horizontal";
}) {
  const pathname = usePathname();

  return (
    <nav aria-label={label} className={orientation === "vertical" ? "lg:w-56 lg:shrink-0" : ""}>
      <ul
        className={cn(
          "flex gap-1",
          orientation === "vertical"
            ? "overflow-x-auto lg:flex-col lg:overflow-visible"
            : "flex-wrap",
        )}
      >
        {items.map((item) => {
          if (item.status === "pending") {
            return (
              <li key={item.href} className="shrink-0">
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

          const current = pathname === item.href;
          return (
            <li key={item.href} className="shrink-0">
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
        })}
      </ul>
    </nav>
  );
}
