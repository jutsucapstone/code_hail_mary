import Image from "next/image";

import { WordmarkArt } from "@/components/site/wordmark-art";
import { cn } from "@/lib/utils";

import logoSrc from "@/public/jutsu-logo.png";

/**
 * The supplied logo, used verbatim — no redraw, no recolour.
 *
 * The source PNG is 1254² with a real alpha channel, so it sits on both the
 * obsidian and off-white grounds untouched. It is decorative wherever a text
 * wordmark sits beside it, so `alt` is empty by default and the adjacent link
 * carries the accessible name; pass `alt` when the mark stands alone.
 */
export function Logo({
  className,
  alt = "",
  priority = false,
}: {
  className?: string;
  alt?: string;
  priority?: boolean;
}) {
  return (
    <Image
      src={logoSrc}
      alt={alt}
      priority={priority}
      // Rendered at ~28-40px; cap the decode work well above that for retina.
      sizes="96px"
      className={cn("h-7 w-7 select-none object-contain", className)}
    />
  );
}

/**
 * The JUTSU lockup — the supplied artwork itself, vector-traced.
 *
 * Sized by *width* (the art has a fixed 1000:485 aspect), so callers pass e.g.
 * `w-28` rather than a font size. The accessible name comes from the SVG, so it
 * still reads as "JUTSU" to assistive tech and search engines.
 */
export function Wordmark({
  className,
  gradient = true,
}: {
  className?: string;
  /** Flat `currentColor` instead of the green→neutral fade. */
  gradient?: boolean;
}) {
  return <WordmarkArt gradient={gradient} className={className} />;
}
