"use client";

import { motion, useReducedMotion } from "framer-motion";

import { cn } from "@/lib/utils";

/**
 * Heading that resolves word by word as it enters the viewport.
 *
 * Words are the unit, not characters: per-character staggers on a full sentence
 * read as noise and blow up the node count. Each word keeps a normal space
 * around it so the text still wraps and copies correctly, and the whole thing
 * collapses to a plain heading under `prefers-reduced-motion`.
 */
export function AnimatedHeading({
  text,
  className,
  id,
  delay = 0,
  highlightFrom,
}: {
  text: string;
  className?: string;
  id?: string;
  delay?: number;
  /** Word index from which the remainder is set in muted type. */
  highlightFrom?: number;
}) {
  const shouldReduceMotion = useReducedMotion();
  const words = text.split(" ");
  const muted = (i: number) => highlightFrom !== undefined && i >= highlightFrom;

  if (shouldReduceMotion) {
    return (
      <h2 id={id} className={className}>
        {highlightFrom === undefined
          ? text
          : words.map((word, i) => (
              <span key={`${word}-${i}`} className={muted(i) ? "text-muted-foreground/65" : undefined}>
                {word}{" "}
              </span>
            ))}
      </h2>
    );
  }

  return (
    <h2 id={id} className={className}>
      <motion.span
        aria-hidden="true"
        initial="hidden"
        whileInView="shown"
        viewport={{ once: true, margin: "-12% 0px" }}
        transition={{ staggerChildren: 0.045, delayChildren: delay }}
        className="inline"
      >
        {words.map((word, i) => (
          <motion.span
            key={`${word}-${i}`}
            variants={{
              hidden: { opacity: 0, y: "0.4em", filter: "blur(6px)" },
              shown: { opacity: 1, y: "0em", filter: "blur(0px)" },
            }}
            transition={{ duration: 0.55, ease: [0.22, 1, 0.36, 1] }}
            className={cn(
              "inline-block whitespace-pre",
              muted(i) && "text-muted-foreground/65",
            )}
          >
            {word}{" "}
          </motion.span>
        ))}
      </motion.span>
      {/* The animated copy is decorative; this keeps the heading readable to AT. */}
      <span className="sr-only">{text}</span>
    </h2>
  );
}
