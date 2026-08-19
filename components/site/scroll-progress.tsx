"use client";

import { motion, useReducedMotion, useScroll, useSpring } from "framer-motion";

/**
 * Reading-progress hairline pinned under the header.
 *
 * `offset: [0, 1]` for the same reason the text reveal needs it — an omitted
 * offset lets framer accelerate onto a native ViewTimeline, and the JS
 * MotionValue this `useSpring` reads from then never updates.
 */
export function ScrollProgress() {
  const shouldReduceMotion = useReducedMotion();
  const { scrollYProgress } = useScroll({ offset: [0, 1] });
  const scaleX = useSpring(scrollYProgress, {
    stiffness: 180,
    damping: 30,
    restDelta: 0.001,
  });

  return (
    <motion.div
      aria-hidden="true"
      style={{ scaleX: shouldReduceMotion ? scrollYProgress : scaleX }}
      className="absolute inset-x-0 bottom-0 h-px origin-left bg-gradient-to-r from-brand via-brand to-graph"
    />
  );
}
