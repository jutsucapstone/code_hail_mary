"use client";

import { useRef } from "react";
import { motion, useReducedMotion, useScroll, useSpring, useTransform } from "framer-motion";

/**
 * Parallax backdrop for the hero: a hairline grid and two brand glows that
 * drift at different rates as the page scrolls, giving the section depth
 * instead of a flat painted panel.
 *
 * `offset` is spelled numerically for the same reason as elsewhere — a named
 * offset lets framer accelerate onto a ViewTimeline and the JS MotionValue the
 * transforms read from then stops updating.
 */
export function HeroAtmosphere() {
  const ref = useRef<HTMLDivElement | null>(null);
  const shouldReduceMotion = useReducedMotion();

  const { scrollYProgress } = useScroll({
    target: ref,
    offset: [0, 1],
  });
  const soft = useSpring(scrollYProgress, { stiffness: 90, damping: 26, restDelta: 0.001 });

  const warmY = useTransform(soft, [0, 1], ["0%", "38%"]);
  const coolY = useTransform(soft, [0, 1], ["0%", "-22%"]);
  const gridY = useTransform(soft, [0, 1], ["0%", "14%"]);
  const fade = useTransform(soft, [0, 0.85], [1, 0.25]);

  if (shouldReduceMotion) {
    return (
      <div aria-hidden="true" className="absolute inset-0 -z-10 overflow-clip">
        <div className="hairline-grid radial-fade absolute inset-0" />
        <div className="glow-cool absolute -top-40 right-[-10%] h-[34rem] w-[34rem] rounded-full blur-[130px]" />
        <div className="glow-warm absolute -top-24 left-[-8%] h-[30rem] w-[38rem] rounded-full blur-[130px]" />
      </div>
    );
  }

  return (
    <div ref={ref} aria-hidden="true" className="absolute inset-0 -z-10 overflow-clip">
      <motion.div style={{ y: gridY, opacity: fade }} className="absolute inset-0">
        <div className="hairline-grid radial-fade absolute inset-0" />
      </motion.div>
      <motion.div
        style={{ y: coolY }}
        className="glow-cool absolute -top-40 right-[-10%] h-[34rem] w-[34rem] rounded-full blur-[130px]"
      />
      <motion.div
        style={{ y: warmY }}
        className="glow-warm absolute -top-24 left-[-8%] h-[30rem] w-[38rem] rounded-full blur-[130px]"
      />
    </div>
  );
}
