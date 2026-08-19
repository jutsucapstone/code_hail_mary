"use client";

import { useState } from "react";
import { Plus } from "lucide-react";
import { motion, useReducedMotion } from "framer-motion";

import { Reveal } from "@/components/site/reveal";
import { Section, SectionHeading } from "@/components/site/section";
import { faq } from "@/lib/content";
import { cn } from "@/lib/utils";

/**
 * Disclosure list rather than a single-open accordion: readers comparing two
 * answers should not have to keep re-opening the first one.
 *
 * Every panel stays mounted. Unmounting the collapsed ones left each trigger's
 * `aria-controls` pointing at an element that was not in the document, and kept
 * the answers out of the HTML crawlers read. Closed panels collapse to zero
 * height and are marked `inert`, which removes them from the tab order and the
 * accessibility tree without removing them from the DOM.
 */
export function FaqSection() {
  const [open, setOpen] = useState<Set<number>>(() => new Set([0]));
  const shouldReduceMotion = useReducedMotion();

  const toggle = (i: number) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });

  return (
    <Section id="faq" className="border-t border-hairline">
      <SectionHeading id="faq" eyebrow={faq.eyebrow} title={faq.title} />

      <Reveal delay={0.06} className="mt-14">
        <dl className="divide-y divide-hairline border-y border-hairline">
          {faq.items.map((item, i) => {
            const isOpen = open.has(i);
            return (
              <div key={item.q}>
                <dt>
                  <button
                    type="button"
                    onClick={() => toggle(i)}
                    aria-expanded={isOpen}
                    aria-controls={`faq-panel-${i}`}
                    id={`faq-trigger-${i}`}
                    className="group flex w-full items-start justify-between gap-6 py-6 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                  >
                    <span
                      className={cn(
                        "text-pretty text-base font-medium tracking-tight transition-colors duration-300 sm:text-lg",
                        isOpen ? "text-brand" : "text-foreground group-hover:text-brand",
                      )}
                    >
                      {item.q}
                    </span>
                    <span
                      className={cn(
                        "mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border transition-all duration-300",
                        isOpen
                          ? "rotate-45 border-brand/40 bg-brand/10 text-brand"
                          : "border-hairline-strong text-muted-foreground group-hover:border-brand/40 group-hover:text-brand",
                      )}
                    >
                      <Plus aria-hidden="true" className="size-3.5" />
                    </span>
                  </button>
                </dt>

                <motion.dd
                  id={`faq-panel-${i}`}
                  aria-labelledby={`faq-trigger-${i}`}
                  inert={!isOpen}
                  initial={false}
                  animate={
                    shouldReduceMotion
                      ? { height: isOpen ? "auto" : 0 }
                      : { height: isOpen ? "auto" : 0, opacity: isOpen ? 1 : 0 }
                  }
                  transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
                  className="overflow-hidden"
                >
                  <p className="max-w-3xl pb-6 pr-12 text-pretty text-sm leading-relaxed text-muted-foreground lg:text-[0.9375rem]">
                    {item.a}
                  </p>
                </motion.dd>
              </div>
            );
          })}
        </dl>
      </Reveal>
    </Section>
  );
}
