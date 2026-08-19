import { Plug, Share2, Sparkles } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { Reveal } from "@/components/site/reveal";
import { Section, SectionHeading } from "@/components/site/section";
import { SpotlightGroup } from "@/components/site/spotlight-group";
import { howItWorks } from "@/lib/content";

const ICONS: Record<string, LucideIcon> = {
  connect: Plug,
  build: Share2,
  ask: Sparkles,
};

export function HowItWorksSection() {
  return (
    <Section id="how-it-works" className="border-t border-hairline">
      <SectionHeading
        id="how-it-works"
        eyebrow={howItWorks.eyebrow}
        title={howItWorks.title}
        lead={howItWorks.lead}
      />

      <SpotlightGroup
        as="ol"
        className="mt-16 grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline lg:grid-cols-3"
      >
        {howItWorks.steps.map((step, index) => {
          const Icon = ICONS[step.id];
          return (
            <Reveal
              as="li"
              key={step.id}
              delay={index * 0.07}
              className="spotlight group flex flex-col gap-5 bg-background p-7 lg:p-9"
            >
              <div className="flex items-center justify-between gap-4">
                <span className="flex size-11 items-center justify-center rounded-xl border border-hairline-strong bg-surface text-brand transition-colors duration-300 group-hover:border-brand/40">
                  <Icon aria-hidden="true" className="size-5" />
                </span>
                <span
                  aria-hidden="true"
                  className="font-mono text-4xl font-semibold leading-none text-hairline-strong transition-colors duration-300 group-hover:text-brand/25"
                >
                  {step.index}
                </span>
              </div>

              <div>
                <h3 className="display text-xl font-semibold sm:text-[1.375rem]">
                  {step.title}
                </h3>
                <p className="mt-3 text-pretty text-sm leading-relaxed text-muted-foreground lg:text-[0.9375rem]">
                  {step.body}
                </p>
              </div>

              <p className="mt-auto border-t border-hairline pt-4 font-mono text-xs text-muted-foreground/80">
                {step.detail}
              </p>
            </Reveal>
          );
        })}
      </SpotlightGroup>
    </Section>
  );
}
