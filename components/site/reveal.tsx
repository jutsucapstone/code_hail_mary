"use client";

import { type ElementType, type ReactNode } from "react";
import { motion, useReducedMotion } from "framer-motion";

import { cn } from "@/lib/utils";

interface RevealProps {
  children: ReactNode;
  className?: string;
  /** Stagger in seconds. Keep under ~0.4s total so content never feels gated. */
  delay?: number;
  /** Travel distance in px. 0 gives a pure cross-fade. */
  y?: number;
  as?: ElementType;
}

/**
 * One-shot fade-and-rise as an element scrolls into view.
 *
 * Deliberately `once: true` — re-animating on every pass makes long pages feel
 * twitchy and re-triggers on scroll-up. Collapses to a no-op animation when the
 * visitor has asked for reduced motion.
 */
export function Reveal({
  children,
  className,
  delay = 0,
  y = 18,
  as = "div",
}: RevealProps) {
  const shouldReduceMotion = useReducedMotion();
  const MotionTag = motion[as as keyof typeof motion] as typeof motion.div;

  if (shouldReduceMotion) {
    const Tag = as;
    return <Tag className={className}>{children}</Tag>;
  }

  return (
    <MotionTag
      className={cn(className)}
      initial={{ opacity: 0, y }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-12% 0px -12% 0px" }}
      transition={{ duration: 0.6, delay, ease: [0.22, 1, 0.36, 1] }}
    >
      {children}
    </MotionTag>
  );
}
