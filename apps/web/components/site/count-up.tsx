"use client";

import { useEffect, useRef, useState } from "react";
import { useInView, useMotionValueEvent, useReducedMotion, useSpring } from "framer-motion";

interface CountUpProps {
  value: number;
  decimals?: number;
  prefix?: string;
  suffix?: string;
  durationMs?: number;
}

/**
 * Counts a figure up once, the first time it scrolls into view.
 *
 * The full formatted value is always present in the DOM for assistive tech and
 * for anyone with `prefers-reduced-motion` set — the animation only ever
 * replaces the *visible* digits, so the number can never be announced wrong or
 * left mid-count.
 *
 * The displayed number is *derived*, not stored on a second state hop: the
 * effect only pushes the spring's target (an external system), and the spring's
 * own subscription is what feeds React. That keeps setState out of the effect
 * body and avoids a cascading render per stat.
 */
export function CountUp({
  value,
  decimals = 0,
  prefix = "",
  suffix = "",
  durationMs = 1400,
}: CountUpProps) {
  const ref = useRef<HTMLSpanElement | null>(null);
  const shouldReduceMotion = useReducedMotion();
  const inView = useInView(ref, { once: true, margin: "-15% 0px" });

  // null until the spring emits its first frame, so the pre-scroll render is 0.
  const [animated, setAnimated] = useState<number | null>(null);

  // A spring reads better than a linear tween: quick out of the gate, settling
  // into the final digit instead of stopping dead on it.
  const spring = useSpring(0, { duration: durationMs, bounce: 0 });

  useMotionValueEvent(spring, "change", (latest) => {
    setAnimated(latest as number);
  });

  useEffect(() => {
    if (!inView || shouldReduceMotion) return;
    spring.set(value);
  }, [inView, shouldReduceMotion, value, spring]);

  const format = (n: number) =>
    `${prefix}${n.toLocaleString("en-US", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })}${suffix}`;

  const shown = shouldReduceMotion ? value : (animated ?? 0);

  return (
    <span ref={ref}>
      {/* Screen readers and no-JS get the real figure; the ticking copy is decorative. */}
      <span className="sr-only">{format(value)}</span>
      <span aria-hidden="true" className="tabular-nums">
        {format(shown)}
      </span>
    </span>
  );
}
