import { Sparkles } from "lucide-react";

import { PrimaryCta, SecondaryCta } from "@/components/site/cta-buttons";
import { Logo, Wordmark } from "@/components/site/logo";
import { MemoryGraph } from "@/components/site/memory-graph";
import { HeroAtmosphere } from "@/components/site/hero-atmosphere";
import { Reveal } from "@/components/site/reveal";
import { SpotlightGroup } from "@/components/site/spotlight-group";
import { Container } from "@/components/site/section";
import { hero, memoryCore } from "@/lib/content";

const LEGEND = [
  { label: "People", color: "var(--brand)" },
  { label: "Projects", color: "var(--foreground)" },
  { label: "Decisions", color: "var(--graph)" },
  { label: "Skills", color: "var(--graph-muted)" },
] as const;

export function Hero() {
  return (
    <section id="hero" aria-labelledby="hero-heading" className="relative isolate pt-12 lg:pt-14">
      <HeroAtmosphere />

      <Container>
        <div className="grid items-start gap-14 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)] lg:gap-16">
          <div className="flex flex-col items-start">
            <Reveal>
              <p className="inline-flex items-center gap-2 rounded-full border border-hairline-strong bg-surface/70 px-3.5 py-1.5 backdrop-blur">
                <Sparkles aria-hidden="true" className="size-3.5 text-brand" />
                <span className="eyebrow text-muted-foreground">{hero.badge}</span>
              </p>
            </Reveal>

            <Reveal delay={0.06} className="mt-8">
              {/* The lockup owns the gradient here, so the tagline stays plain —
                  two gradients stacked read as a mistake. */}
              <h1 id="hero-heading">
                <Wordmark className="w-[min(100%,20rem)] sm:w-[26rem] lg:w-[32rem]" />
                <span className="display mt-4 block text-2xl font-semibold text-foreground/85 sm:text-3xl lg:text-[2.125rem]">
                  {hero.tagline}
                </span>
              </h1>
            </Reveal>

            <Reveal delay={0.12} className="mt-7 max-w-xl">
              <p className="text-pretty text-base leading-relaxed text-muted-foreground sm:text-lg">
                {hero.subhead}
              </p>
            </Reveal>

            <Reveal delay={0.18} className="mt-9 flex flex-col gap-3 sm:flex-row sm:items-center">
              <PrimaryCta href={hero.primaryCta.href}>{hero.primaryCta.label}</PrimaryCta>
              <SecondaryCta href={hero.secondaryCta.href}>
                {hero.secondaryCta.label}
              </SecondaryCta>
            </Reveal>

            <Reveal delay={0.24} className="mt-10 w-full">
              <p className="eyebrow text-muted-foreground/80">{hero.kicker}</p>
            </Reveal>
          </div>

          <Reveal delay={0.1} y={26} className="relative">
            <SpotlightGroup>
            <figure className="spotlight relative rounded-2xl border border-hairline-strong bg-surface/60 p-1.5 backdrop-blur-sm">
              <div className="rounded-xl border border-hairline bg-background/40">
                <div className="flex items-center justify-between gap-4 border-b border-hairline px-4 py-3">
                  <span className="flex items-center gap-2">
                    <Logo className="h-4 w-4" />
                    <span className="eyebrow text-muted-foreground">{memoryCore.title}</span>
                  </span>
                  <span className="flex items-center gap-1.5">
                    <span className="size-1.5 animate-pulse rounded-full bg-brand" />
                    <span className="eyebrow text-brand">Live</span>
                  </span>
                </div>

                <div className="px-3 py-4 sm:px-4">
                  <MemoryGraph
                    hint={memoryCore.hint}
                    className="h-[24rem] sm:h-[27rem] lg:h-[30rem]"
                  />
                </div>

                <figcaption className="flex flex-wrap items-center justify-between gap-x-5 gap-y-2 border-t border-hairline px-4 py-3.5">
                  <span className="flex flex-wrap items-center gap-x-5 gap-y-2">
                    {LEGEND.map((item) => (
                      <span key={item.label} className="flex items-center gap-2">
                        <span
                          aria-hidden="true"
                          className="size-2 rounded-full ring-1 ring-inset ring-hairline-strong"
                          style={{ backgroundColor: item.color }}
                        />
                        <span className="text-xs text-muted-foreground">{item.label}</span>
                      </span>
                    ))}
                  </span>
                  <span className="font-mono text-[0.6875rem] text-muted-foreground/80">
                    {memoryCore.substrate}
                  </span>
                </figcaption>
              </div>
            </figure>
            </SpotlightGroup>
          </Reveal>
        </div>
      </Container>

      <StackMarquee />
    </section>
  );
}

function StackMarquee() {
  return (
    <div className="mt-16 border-y border-hairline py-5 lg:mt-20">
      <h2 className="sr-only">Technology stack</h2>
      <div className="mask-fade-x overflow-clip">
        <ul className="animate-marquee flex w-max items-center gap-10 pr-10 motion-reduce:animate-none">
          {[0, 1].map((copy) => (
            // The list is rendered twice so the -50% translate loops seamlessly.
            <li key={copy} className="flex items-center gap-10" aria-hidden={copy === 1}>
              {hero.stack.map((item) => (
                <span
                  key={`${copy}-${item}`}
                  className="flex items-center gap-10 whitespace-nowrap font-mono text-[0.8125rem] tracking-tight text-muted-foreground/80"
                >
                  {item}
                  <span aria-hidden="true" className="size-1 rounded-full bg-hairline-strong" />
                </span>
              ))}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
