import { Check, Minus, X } from "lucide-react";

import { Wordmark } from "@/components/site/logo";
import { Reveal } from "@/components/site/reveal";
import { Section, SectionHeading } from "@/components/site/section";
import { landscape } from "@/lib/content";

type Support = boolean | "partial";

function SupportCell({ value, emphasis }: { value: Support; emphasis: boolean }) {
  const label =
    value === true ? "Supported" : value === "partial" ? "Partial" : "Not supported";

  return (
    <span className="flex items-center justify-center">
      <span className="sr-only">{label}</span>
      {value === true ? (
        <Check
          aria-hidden="true"
          className={emphasis ? "size-4.5 text-brand" : "size-4 text-foreground/55"}
        />
      ) : value === "partial" ? (
        <Minus aria-hidden="true" className="size-4 text-muted-foreground/80" />
      ) : (
        <X aria-hidden="true" className="size-4 text-muted-foreground/70" />
      )}
    </span>
  );
}

export function LandscapeSection() {
  return (
    <Section id="landscape" className="border-t border-hairline">
      <SectionHeading
        id="landscape"
        eyebrow={landscape.eyebrow}
        title={landscape.title}
        lead={landscape.lead}
      />

      <Reveal delay={0.08} className="mt-14">
        {/* Horizontal scroll is contained to the table so the page body never
            scrolls sideways on narrow viewports.
            `relative` is load-bearing: a static scroll box is not a containing
            block, so the 46rem min-width propagated out to the root, widened the
            initial containing block, and stretched the fixed header past 100vw. */}
        <div className="relative overflow-x-auto rounded-2xl border border-hairline-strong">
          <table className="w-full min-w-[46rem] border-collapse text-sm">
            <caption className="sr-only">
              Capability comparison between JUTSU and existing enterprise knowledge tools
            </caption>
            <thead>
              <tr className="border-b border-hairline">
                <th
                  scope="col"
                  className="w-[38%] px-6 py-4 text-left font-medium text-muted-foreground"
                >
                  Capability
                </th>
                {landscape.columns.map((column, index) => (
                  <th
                    key={column}
                    scope="col"
                    className={[
                      "px-4 py-4 text-center text-[0.8125rem] font-semibold tracking-tight",
                      index === 0 ? "bg-brand/6 text-brand" : "text-foreground/70",
                    ].join(" ")}
                  >
                    {/* Our own column carries the lockup; competitors stay plain. */}
                    {index === 0 ? (
                      <Wordmark gradient={false} className="mx-auto w-16 text-brand" />
                    ) : (
                      column
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {landscape.rows.map((row) => (
                <tr key={row.capability} className="border-b border-hairline last:border-b-0">
                  <th
                    scope="row"
                    className="px-6 py-4 text-left text-[0.8125rem] font-normal leading-snug text-foreground/85 sm:text-sm"
                  >
                    {row.capability}
                  </th>
                  {row.values.map((value, index) => (
                    <td
                      key={`${row.capability}-${index}`}
                      className={index === 0 ? "bg-brand/6 px-4 py-4" : "px-4 py-4"}
                    >
                      <SupportCell value={value as Support} emphasis={index === 0} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Reveal>

      <Reveal delay={0.1} className="mt-8 flex flex-col gap-4 border-t border-hairline pt-8">
        <p className="display max-w-3xl text-xl font-medium text-foreground/90 sm:text-2xl">
          {landscape.moat}
        </p>
        <p className="max-w-3xl text-xs leading-relaxed text-muted-foreground/80">
          {landscape.footnote}
        </p>
      </Reveal>
    </Section>
  );
}
