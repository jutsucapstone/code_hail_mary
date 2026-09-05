"use client";

import { useEffect, useRef, useState } from "react";
import { motion, useSpring, useTransform, type SpringOptions } from "framer-motion";

import { cn } from "@/lib/utils";

/**
 * A cursor-following glow that lights the surface it is dropped into.
 *
 * It positions itself against its own parent rather than taking a ref, so a caller adds
 * one line to a panel and nothing else. Two departures from the widely-copied original,
 * both defects rather than taste:
 *
 * * **The listeners are named.** The original registers `() => setIsHovered(true)` and
 *   then calls `removeEventListener` with a *freshly constructed* arrow function, which
 *   matches nothing — every mount leaked three listeners on a node that outlives it.
 * * **The light is painted from an explicit colour, not Tailwind gradient stops.** The
 *   original leans on `--tw-gradient-stops`, whose contract changed in Tailwind v4; a
 *   spotlight that silently renders transparent is worse than one that cannot be
 *   restyled by class. `color` takes any CSS colour, which is also what the published
 *   demo means by its `fill` prop.
 *
 * The parent is nudged to `position: relative` and `overflow: hidden` only when it is
 * not already positioned or clipping — a parent that deliberately overflows, or one
 * using `overflow-clip`, keeps what it declared.
 */
interface SpotlightProps {
  className?: string;
  /** Diameter of the light, in pixels. */
  size?: number;
  /** Centre colour of the light; it fades to transparent at the rim. */
  color?: string;
  springOptions?: SpringOptions;
}

export function Spotlight({
  className,
  size = 240,
  color = "rgba(255,255,255,0.45)",
  springOptions = { bounce: 0 },
}: SpotlightProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [parent, setParent] = useState<HTMLElement | null>(null);
  const [isHovered, setIsHovered] = useState(false);

  const mouseX = useSpring(0, springOptions);
  const mouseY = useSpring(0, springOptions);
  const left = useTransform(mouseX, (x) => `${x - size / 2}px`);
  const top = useTransform(mouseY, (y) => `${y - size / 2}px`);

  useEffect(() => {
    const element = containerRef.current?.parentElement ?? null;
    if (element === null) return;
    const computed = window.getComputedStyle(element);
    if (computed.position === "static") element.style.position = "relative";
    if (computed.overflow === "visible") element.style.overflow = "hidden";
    setParent(element);
  }, []);

  useEffect(() => {
    if (parent === null) return;

    const handleMove = (event: MouseEvent) => {
      const bounds = parent.getBoundingClientRect();
      mouseX.set(event.clientX - bounds.left);
      mouseY.set(event.clientY - bounds.top);
    };
    const handleEnter = () => setIsHovered(true);
    const handleLeave = () => setIsHovered(false);

    parent.addEventListener("mousemove", handleMove);
    parent.addEventListener("mouseenter", handleEnter);
    parent.addEventListener("mouseleave", handleLeave);
    return () => {
      parent.removeEventListener("mousemove", handleMove);
      parent.removeEventListener("mouseenter", handleEnter);
      parent.removeEventListener("mouseleave", handleLeave);
    };
  }, [parent, mouseX, mouseY]);

  return (
    <motion.div
      ref={containerRef}
      aria-hidden="true"
      className={cn(
        "pointer-events-none absolute z-10 rounded-full blur-2xl transition-opacity duration-300",
        isHovered ? "opacity-100" : "opacity-0",
        className,
      )}
      style={{
        width: size,
        height: size,
        left,
        top,
        background: `radial-gradient(circle at center, ${color}, transparent 80%)`,
      }}
    />
  );
}
