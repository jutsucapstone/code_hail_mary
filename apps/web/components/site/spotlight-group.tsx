"use client";

import { useCallback, useRef, type ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * Writes the cursor position into `--mx` / `--my` on each `.spotlight` child so
 * the CSS gradient can follow it.
 *
 * One listener for the whole group rather than one per card, coalesced into a
 * single rAF so a grid of cards costs one style write per frame instead of N.
 * Pointer-only: `pointerType === "mouse"` skips the work on touch, where there
 * is no hover to reveal anyway.
 */
export function SpotlightGroup({
  children,
  className,
  as: Tag = "div",
}: {
  children: ReactNode;
  className?: string;
  as?: "div" | "ul" | "ol" | "dl";
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const frame = useRef(0);
  const pending = useRef<{ x: number; y: number } | null>(null);

  const onPointerMove = useCallback((event: React.PointerEvent) => {
    if (event.pointerType !== "mouse") return;
    pending.current = { x: event.clientX, y: event.clientY };
    if (frame.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = 0;
      const point = pending.current;
      const root = ref.current;
      if (!point || !root) return;
      for (const card of root.querySelectorAll<HTMLElement>(".spotlight")) {
        const box = card.getBoundingClientRect();
        card.style.setProperty("--mx", `${point.x - box.left}px`);
        card.style.setProperty("--my", `${point.y - box.top}px`);
      }
    });
  }, []);

  return (
    <Tag
      ref={ref as never}
      onPointerMove={onPointerMove}
      className={cn("spotlight-group", className)}
    >
      {children}
    </Tag>
  );
}
