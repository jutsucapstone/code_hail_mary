import { cn } from "@/lib/utils"

/**
 * A placeholder box while content loads.
 *
 * Two departures from the stock shadcn component, both deliberate:
 *
 * `aria-hidden`, because a skeleton is decoration. The announcement belongs to the
 * `LoadingRegion` that wraps it — a screen reader hearing a row of empty boxes is worse
 * off than one hearing "Loading employees".
 *
 * `motion-reduce:animate-none`, because a pulsing rectangle is exactly the kind of
 * ambient motion `prefers-reduced-motion` exists to switch off.
 *
 * A note on why this is `animate-pulse` and not the `shimmer` utility the `radix-nova`
 * preset ships: `shimmer` sets `background-clip: text` with a transparent text fill, so
 * it clips to glyphs. On an empty placeholder box that renders literally nothing, and
 * the loading state ships invisible.
 */
function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      aria-hidden="true"
      className={cn(
        "animate-pulse rounded-xl bg-surface/60 motion-reduce:animate-none",
        className,
      )}
      {...props}
    />
  )
}

export { Skeleton }
