import { ArchitectureSection } from "@/components/site/architecture-section";
import { ContactSection } from "@/components/site/contact-section";
import { FaqSection } from "@/components/site/faq-section";
import { HowItWorksSection } from "@/components/site/how-it-works-section";
import { Hero } from "@/components/site/hero";
import { LandscapeSection } from "@/components/site/landscape-section";
import { OpeningStatement } from "@/components/site/opening-statement";
import { ProblemSection } from "@/components/site/problem-section";
import { ScrollReset } from "@/components/site/scroll-reset";
import { SiteFooter } from "@/components/site/site-footer";
import { SiteChrome } from "@/components/site/site-chrome";
import { MAIN_CONTENT_ID } from "@/lib/landmarks";

export default function LandingPage() {
  return (
    <>
      <ScrollReset />
      <SiteChrome />
      <main id={MAIN_CONTENT_ID} className="flex-1">
        {/* The thesis, before anything is sold. */}
        <OpeningStatement />

        <Hero />
        <ProblemSection />
        <HowItWorksSection />
        <ArchitectureSection />
        <LandscapeSection />
        <FaqSection />
        <ContactSection />
      </main>
      <SiteFooter />
    </>
  );
}
