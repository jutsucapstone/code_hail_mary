import type { ReactNode } from "react";

import { AnimatedHeading } from "@/components/site/animated-heading";
import { Reveal } from "@/components/site/reveal";
import { cn } from "@/lib/utils";

export function Section({
  id,
  children,
  className,
  bleed = false,
}: {
  id: string;
  children: ReactNode;
  className?: string;
  /** Skip the standard container so the section can manage its own gutters. */
  bleed?: boolean;
}) {
  return (
    <section
      id={id}
      aria-labelledby={`${id}-heading`}
      className={cn("relative py-11 sm:py-12 lg:py-16", className)}
    >
      {bleed ? children : <Container>{children}</Container>}
    </section>
  );
}

export function Container({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("mx-auto w-full max-w-7xl px-6 lg:px-8", className)}>
      {children}
    </div>
  );
}

export function SectionHeading({
  id,
  eyebrow,
  title,
  accent,
  lead,
  align = "left",
  className,
}: {
  id: string;
  eyebrow: string;
  title: string;
  /** Trailing clause, set in muted type and animated after the title. */
  accent?: string;
  lead?: string;
  align?: "left" | "center";
  className?: string;
}) {
  return (
    <Reveal
      className={cn(
        "flex flex-col gap-5",
        align === "center" ? "mx-auto max-w-3xl text-center" : "max-w-3xl",
        className,
      )}
    >
      <span
        className={cn(
          "eyebrow inline-flex items-center gap-2.5 text-brand",
          align === "center" && "justify-center",
        )}
      >
        <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
        {eyebrow}
      </span>
      <AnimatedHeading
        id={`${id}-heading`}
        text={accent ? `${title} ${accent}` : title}
        highlightFrom={accent ? title.split(" ").length : undefined}
        className="display text-4xl font-semibold sm:text-5xl lg:text-[3.25rem]"
      />
      {lead ? (
        <p
          className={cn(
            "max-w-2xl text-pretty text-base leading-relaxed text-muted-foreground sm:text-lg",
            align === "center" && "mx-auto",
          )}
        >
          {lead}
        </p>
      ) : null}
    </Reveal>
  );
}
