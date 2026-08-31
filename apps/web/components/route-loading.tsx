import { LoadingRegion, Skeleton } from "@/components/states";

/**
 * The instant fallback a route segment shows while its page streams in.
 *
 * A server component on purpose — it holds no state and no handlers, so there is no
 * reason to ship it to the browser. Next prefetches this, which is what makes moving
 * between console sections feel immediate rather than blank.
 *
 * The boxes are `aria-hidden` (see `ui/skeleton.tsx`); the sentence in `LoadingRegion` is
 * the whole of what a screen reader gets, and without it the page is simply silent while
 * it loads — indistinguishable from nothing happening.
 *
 * `rows` is the only knob. A skeleton whose shape is wildly unlike the content that
 * replaces it makes the swap feel like a second page load, so each caller asks for
 * roughly what it is about to render rather than sharing one average.
 */
export function RouteLoading({
  label,
  rows = 3,
  wide = false,
}: {
  label: string;
  rows?: number;
  /** Wide surfaces lead with a stat strip; prose surfaces do not. */
  wide?: boolean;
}) {
  return (
    <LoadingRegion label={label}>
      <div aria-hidden="true" className="flex flex-col gap-6">
        <Skeleton className="h-9 w-56" />

        {wide ? (
          <div className="grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline sm:grid-cols-3">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-28 bg-background" />
            ))}
          </div>
        ) : null}

        <div className="flex flex-col gap-3">
          {Array.from({ length: rows }, (_, i) => (
            <Skeleton key={i} className="h-14 w-full" />
          ))}
        </div>
      </div>
    </LoadingRegion>
  );
}
