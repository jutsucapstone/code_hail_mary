"use client";

import { FC, ReactNode, useRef } from "react";
import {
  motion,
  MotionValue,
  useReducedMotion,
  useScroll,
  useTransform,
} from "framer-motion";

import { cn } from "@/lib/utils";

interface TextRevealByWordProps {
  text: string;
  className?: string;
  /**
   * Scroll progress (0–1) at which the last word reaches full opacity.
   *
   * Anything below 1 buys a "dwell" beat: the sentence finishes lighting up
   * while still pinned, so it can actually be read before it scrolls away.
   * At 1 the final word lands on the exact frame the sticky pin releases.
   */
  completeAt?: number;
}

/**
 * Scroll-linked word-by-word reveal.
 *
 * Four corrections against the upstream Magic UI snippet:
 *  1. `targetRef` is bound only to the tall outer container. Upstream also bound
 *     it to the inner `<p>`, and because a ref callback runs per element the
 *     second binding won.  `useScroll` then measured the *sticky* paragraph,
 *     whose viewport rect barely moves, so progress never swept 0 → 1.
 *  2. The reveal completes at `completeAt` rather than at progress 1. Upstream
 *     pairs a 200vh container with a 100vh sticky child, and `useScroll`'s
 *     default offset reaches 1 at `containerH - viewportH` — the very scroll
 *     position where the sticky unpins. The finished sentence was on screen for
 *     zero frames.
 *  3. The mirrored "ghost" copy of each word is `aria-hidden`, so assistive tech
 *     reads the sentence once rather than twice.
 *  4. Under `prefers-reduced-motion` every word resolves to full opacity, so the
 *     text is legible without scrolling.
 *
 * The outer height (`h-[200vh]`) sets how much scroll distance the sweep spans;
 * pass a different height via `className` and `cn()` will override it.
 */
const TextRevealByWord: FC<TextRevealByWordProps> = ({
  text,
  className,
  completeAt = 0.75,
}) => {
  const targetRef = useRef<HTMLDivElement | null>(null);
  const shouldReduceMotion = useReducedMotion();

  const { scrollYProgress } = useScroll({
    target: targetRef,
    /**
     * `[0, 1]` is the numeric spelling of `["start start", "end end"]` — both
     * resolve to a pixel range of 0 → (targetHeight - viewportHeight).
     *
     * The spelling matters. Framer accelerates `useScroll` onto a native
     * ViewTimeline whenever the offset maps to a named CSS range, and an
     * *omitted* offset maps to `contain 0% → contain 100%`. On that path the
     * progress value is driven by WAAPI and the JS `MotionValue` is never
     * written, so every `useTransform` derived from it — i.e. each word's
     * opacity — silently stops tracking. Bare numbers aren't a recognised
     * range, so framer declines to accelerate and uses the JS scroll path,
     * which does update the value the words read.
     */
    offset: [0, 1],
  });
  const words = text.split(" ");

  return (
    <div ref={targetRef} className={cn("relative z-0 h-[200vh]", className)}>
      <div className="sticky top-0 mx-auto flex h-screen max-w-5xl items-center bg-transparent px-4 py-20 sm:px-6 lg:px-8">
        <p
          className={cn(
            // Token-based rather than the upstream `text-black/20 dark:text-white/15`,
            // so the unrevealed state tracks the palette instead of pure b/w.
            "flex flex-wrap text-pretty text-2xl font-semibold tracking-tight text-foreground/15",
            "sm:text-3xl md:text-4xl lg:text-5xl xl:text-[3.5rem] xl:leading-[1.12]",
          )}
        >
          {words.map((word, i) => {
            const span = (1 / words.length) * completeAt;
            const start = i * span;
            const end = start + span;
            return (
              <Word
                key={`${word}-${i}`}
                progress={scrollYProgress}
                range={[start, end]}
                reduceMotion={Boolean(shouldReduceMotion)}
              >
                {word}
              </Word>
            );
          })}
        </p>
      </div>
    </div>
  );
};

interface WordProps {
  children: ReactNode;
  progress: MotionValue<number>;
  range: [number, number];
  reduceMotion?: boolean;
}

const Word: FC<WordProps> = ({ children, progress, range, reduceMotion }) => {
  const opacity = useTransform(progress, range, reduceMotion ? [1, 1] : [0, 1]);

  return (
    <span className="relative mx-1 lg:mx-1.5">
      <span aria-hidden="true" className="absolute opacity-25">
        {children}
      </span>
      <motion.span style={{ opacity }} className="text-foreground">
        {children}
      </motion.span>
    </span>
  );
};

export { TextRevealByWord };
