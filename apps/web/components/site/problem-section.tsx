import { Boxes, Clock, DoorOpen, GraduationCap } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { CountUp } from "@/components/site/count-up";
import { Reveal } from "@/components/site/reveal";
import { SpotlightGroup } from "@/components/site/spotlight-group";
import { Section, SectionHeading } from "@/components/site/section";
import { problem } from "@/lib/content";

const POINT_ICONS: Record<string, LucideIcon> = {
  exits: DoorOpen,
  scatter: Boxes,
  onboarding: GraduationCap,
  blindspot: Clock,
};

export function ProblemSection() {
  return (
    <Section id="problem">
      <SectionHeading
        id="problem"
        eyebrow={problem.eyebrow}
        title={problem.title}
        accent="And nobody notices until it costs."
        lead={problem.lead}
      />

      <SpotlightGroup as="dl" className="mt-16 grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-3">
        {problem.stats.map((stat, index) => (
          <Reveal
            key={stat.id}
            delay={index * 0.07}
            className="spotlight group flex flex-col justify-between gap-6 bg-background p-7 transition-colors duration-300 lg:p-9"
          >
            <div>
              <dd className="display font-mono text-4xl font-semibold text-brand lg:text-5xl">
                <CountUp
                  value={stat.value}
                  decimals={stat.decimals}
                  prefix={stat.prefix}
                  suffix={stat.suffix}
                />
              </dd>
              <p className="mt-2 font-mono text-xs uppercase tracking-[0.14em] text-muted-foreground/80">
                {stat.unit}
              </p>
            </div>
            <div>
              <dt className="text-pretty text-sm leading-relaxed text-foreground/85">
                {stat.label}
              </dt>
              <p className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
                <span
                  aria-hidden="true"
                  className="h-px w-4 bg-hairline-strong transition-all duration-300 group-hover:w-7 group-hover:bg-brand"
                />
                Source: {stat.source}
              </p>
            </div>
          </Reveal>
        ))}
      </SpotlightGroup>

      <ul className="mt-14 grid gap-x-12 gap-y-10 sm:grid-cols-2 lg:mt-16">
        {problem.points.map((point, index) => {
          const Icon = POINT_ICONS[point.id];
          return (
            <Reveal
              as="li"
              key={point.id}
              delay={index * 0.06}
              className="group flex gap-4 border-t border-hairline pt-6"
            >
              <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-lg border border-hairline-strong bg-surface text-brand transition-colors duration-300 group-hover:border-brand/40 group-hover:bg-brand/10">
                <Icon aria-hidden="true" className="size-4.5" />
              </span>
              <div>
                <h3 className="text-base font-semibold tracking-tight">{point.title}</h3>
                <p className="mt-2 text-pretty text-sm leading-relaxed text-muted-foreground">
                  {point.body}
                </p>
              </div>
            </Reveal>
          );
        })}
      </ul>
    </Section>
  );
}
