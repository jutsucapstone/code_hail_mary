import { ShieldCheck } from "lucide-react";

import { ArchitectureExplorer } from "@/components/site/architecture-explorer";
import { Reveal } from "@/components/site/reveal";
import { Section, SectionHeading } from "@/components/site/section";
import { architecture } from "@/lib/content";

export function ArchitectureSection() {
  return (
    <Section id="architecture" className="border-t border-hairline">
      <SectionHeading
        id="architecture"
        eyebrow={architecture.eyebrow}
        title={architecture.title}
        lead={architecture.lead}
      />

      <Reveal delay={0.06} className="mt-16">
        <ArchitectureExplorer />
      </Reveal>

      {/* Security & compliance */}
      <Reveal delay={0.1} className="mt-6">
        <div className="flex flex-col gap-4 rounded-2xl border border-hairline bg-surface/40 px-6 py-5 lg:flex-row lg:items-center lg:gap-8">
          <p className="flex items-center gap-2.5">
            <ShieldCheck aria-hidden="true" className="size-4.5 shrink-0 text-graph" />
            <span className="eyebrow text-graph">Security &amp; compliance</span>
          </p>
          <ul className="flex flex-wrap items-center gap-x-3 gap-y-2">
            {architecture.security.map((item) => (
              <li
                key={item}
                className="rounded-md bg-background/60 px-2.5 py-1 text-xs text-muted-foreground ring-1 ring-inset ring-hairline"
              >
                {item}
              </li>
            ))}
          </ul>
        </div>
      </Reveal>
    </Section>
  );
}
