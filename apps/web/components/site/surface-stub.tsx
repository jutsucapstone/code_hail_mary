import { Container } from "@/components/site/section";
import type { Surface } from "@/lib/surfaces";

/**
 * Placeholder for a surface that is routed but not yet implemented.
 *
 * §4.11 forbids mock data behind a UI surface — unfinished work is flagged off, never
 * faked. So this states plainly what the surface will do and which slice delivers it,
 * rather than rendering invented answers that would look real in a screenshot.
 */
export function SurfaceStub({ surface }: { surface: Surface }) {
  return (
    <Container className="py-16 lg:py-24">
      <div className="max-w-2xl">
        <p className="eyebrow flex items-center gap-2.5 text-brand">
          <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
          {surface.kind === "differentiator" ? "Differentiator" : "Table stakes"}
        </p>

        <h1 className="display mt-5 text-4xl font-semibold sm:text-5xl">{surface.name}</h1>

        <p className="mt-6 text-pretty text-base leading-relaxed text-muted-foreground sm:text-lg">
          {surface.purpose}
        </p>

        <div className="mt-10 rounded-2xl border border-hairline bg-surface/40 p-6">
          <p className="eyebrow text-muted-foreground/80">Not built yet</p>
          <p className="mt-3 text-pretty text-sm leading-relaxed text-muted-foreground">
            This surface is routed and reachable, but has no implementation behind it. It
            lands in slice{" "}
            <span className="font-mono text-foreground">{surface.slice}</span>. Nothing here
            is mocked — when this page shows an answer, that answer will be real and
            cited.
          </p>
        </div>
      </div>
    </Container>
  );
}
