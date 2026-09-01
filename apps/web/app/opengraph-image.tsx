import { ImageResponse } from "next/og";

import { siteConfig } from "@/lib/content";
import { WORDMARK_PATHS, WORDMARK_VIEWBOX } from "@/lib/wordmark-paths";

export const alt = `${siteConfig.name} — ${siteConfig.tagline}`;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

/**
 * The social card, rendered from the same traced lockup the page uses, so a
 * shared link carries the real mark rather than a text approximation.
 *
 * Colours are hard-coded rather than tokenised: this renders outside the DOM,
 * where CSS custom properties do not resolve. They mirror the dark theme.
 */
export default function OpenGraphImage() {
  const BRAND = "#8cd613";
  const INK = "#0a0b0f";

  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          background: INK,
          padding: 72,
          position: "relative",
        }}
      >
        {/* Brand wash, echoing the hero glow */}
        <div
          style={{
            position: "absolute",
            top: -220,
            left: -160,
            width: 900,
            height: 700,
            background: BRAND,
            opacity: 0.1,
            filter: "blur(140px)",
            display: "flex",
          }}
        />

        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <div
            style={{
              display: "flex",
              width: 14,
              height: 14,
              borderRadius: 999,
              background: BRAND,
            }}
          />
          <div
            style={{
              display: "flex",
              color: "#9aa1ab",
              fontSize: 24,
              letterSpacing: 6,
              textTransform: "uppercase",
            }}
          >
            Enterprise Memory OS
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 34 }}>
          <svg width={520} height={252} viewBox={WORDMARK_VIEWBOX}>
            <defs>
              <linearGradient id="og-wordmark" x1="0" y1="0" x2="1" y2="0.35">
                <stop offset="0%" stopColor={BRAND} />
                <stop offset="26%" stopColor={BRAND} />
                <stop offset="58%" stopColor="#5f9e18" />
                <stop offset="100%" stopColor="#f2f4f6" />
              </linearGradient>
            </defs>
            {WORDMARK_PATHS.map((d, i) => (
              <path key={i} d={d} fill="url(#og-wordmark)" />
            ))}
          </svg>

          <div style={{ display: "flex", color: "#f2f4f6", fontSize: 46, letterSpacing: -1 }}>
            {siteConfig.tagline}
          </div>
        </div>

        <div
          style={{
            display: "flex",
            color: "#9aa1ab",
            fontSize: 26,
            borderTop: "1px solid #23262c",
            paddingTop: 26,
          }}
        >
          One living memory of your organisation — people, projects, decisions and skills.
        </div>
      </div>
    ),
    size,
  );
}
