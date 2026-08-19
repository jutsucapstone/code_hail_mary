import { PrimaryCta, SecondaryCta } from "@/components/site/cta-buttons";
import { Reveal } from "@/components/site/reveal";
import { Container } from "@/components/site/section";
import { contact, hero } from "@/lib/content";

export function ContactSection() {
  return (
    <section
      id="contact"
      aria-labelledby="contact-heading"
      className="relative isolate border-t border-hairline py-14 lg:py-20"
    >
      <div aria-hidden="true" className="absolute inset-0 -z-10 overflow-clip">
        <div className="hairline-grid radial-fade absolute inset-0" />
        <div className="glow-warm absolute bottom-[-14rem] left-1/2 h-[30rem] w-[62rem] max-w-[130vw] -translate-x-1/2 rounded-[50%] blur-[130px]" />
      </div>

      <Container>
        <div className="mx-auto max-w-3xl text-center">
          <Reveal>
            <p className="eyebrow inline-flex items-center gap-2.5 text-brand">
              <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
              {contact.eyebrow}
            </p>
          </Reveal>

          <Reveal delay={0.06}>
            <h2
              id="contact-heading"
              className="display mt-6 text-4xl font-semibold sm:text-5xl lg:text-[3.5rem]"
            >
              {contact.title}
            </h2>
          </Reveal>

          <Reveal delay={0.12}>
            <p className="mx-auto mt-6 max-w-2xl text-pretty text-base leading-relaxed text-muted-foreground sm:text-lg">
              {contact.lead}
            </p>
          </Reveal>

          <Reveal
            delay={0.18}
            className="mt-10 flex flex-col items-center justify-center gap-3 sm:flex-row"
          >
            <PrimaryCta href={contact.primaryCta.href}>{contact.primaryCta.label}</PrimaryCta>
            <SecondaryCta href={contact.secondaryCta.href}>
              {contact.secondaryCta.label}
            </SecondaryCta>
          </Reveal>

          <Reveal delay={0.3}>
            <p className="display mt-12 text-lg font-medium text-foreground/60 sm:text-xl">
              {hero.kicker}
            </p>
          </Reveal>
        </div>
      </Container>
    </section>
  );
}
