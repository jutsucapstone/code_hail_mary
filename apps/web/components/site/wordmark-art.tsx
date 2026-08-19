import { siteConfig } from "@/lib/content";
import { WORDMARK_PATHS, WORDMARK_VIEWBOX } from "@/lib/wordmark-paths";
import { cn } from "@/lib/utils";

/**
 * The JUTSU lockup, vector-traced from the supplied artwork.
 *
 * These are the real letterforms, not a lookalike font: the source PNG was
 * thresholded to an ink mask, its boundaries followed as closed contours, and
 * each contour simplified and smoothed into a quadratic path. Five loops out,
 * one per letter. Regenerate with scripts/trace-wordmark.js if the art changes.
 *
 * Vector rather than the raster so it stays crisp at any size and can pick up
 * the theme. The gradient mirrors the original left-to-right green-to-dark fade;
 * the dark end is `--foreground`, which is near-black on the light ground (as
 * in the artwork) and near-white on obsidian, where true black would vanish.
 *
 * The fade is defined once by <WordmarkGradientDefs> rather than inside each
 * instance: the lockup renders three times, and inlining the <defs> emitted the
 * same id three times — invalid, and every copy resolved against the first
 * anyway. A shared def also keeps this a server component, so the ~7.5KB of
 * path data stays in the HTML instead of shipping as client JS.
 */
export function WordmarkArt({
  className,
  gradient = true,
}: {
  className?: string;
  /** Render as one flat colour (inherits `currentColor`) instead of the fade. */
  gradient?: boolean;
}) {
  const fill = gradient ? `url(#${WORDMARK_GRADIENT_ID})` : "currentColor";

  return (
    <svg
      viewBox={WORDMARK_VIEWBOX}
      role="img"
      aria-label={siteConfig.name}
      className={cn("block h-auto w-full", className)}
    >
      {WORDMARK_PATHS.map((d, i) => (
        <path key={i} d={d} fill={fill} />
      ))}
    </svg>
  );
}

/** Single id every gradient-filled lockup points at. */
export const WORDMARK_GRADIENT_ID = "jutsu-wordmark-fade";

/**
 * Renders the wordmark gradient once for the whole document.
 *
 * Mount this near the root. It is sized to zero and positioned out of flow
 * rather than `display: none`, because a display-none SVG does not reliably
 * resolve paint-server references in every engine.
 */
export function WordmarkGradientDefs() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width="0"
      height="0"
      className="absolute"
      style={{ position: "absolute", width: 0, height: 0, overflow: "hidden" }}
    >
      <defs>
        <linearGradient id={WORDMARK_GRADIENT_ID} x1="0" y1="0" x2="1" y2="0.35">
          <stop offset="0%" stopColor="var(--brand)" />
          <stop offset="26%" stopColor="var(--brand)" />
          <stop offset="52%" stopColor="var(--brand-muted)" />
          <stop offset="100%" stopColor="var(--foreground)" />
        </linearGradient>
      </defs>
    </svg>
  );
}
