# JUTSU — landing page

Marketing site for **JUTSU**, an Enterprise Memory OS: one temporal graph of an
organization's people, projects, decisions and skills, with agents that answer
from it with citations.

Corporate Memory Graph · Team Code Hail Mary · Manipal University Jaipur.

## Stack

| | |
|---|---|
| Framework | Next.js 16 (App Router, Turbopack) |
| Language | TypeScript, strict |
| Styling | Tailwind CSS v4 + shadcn/ui (radix / nova preset) |
| Motion | Framer Motion 13 |
| Theming | next-themes, class strategy, light + dark + system |

## Running it

```bash
npm install
npm run dev
```

The dev script pins port **3210**. Port 3000 is deliberately avoided — a
different local app owns that origin and its service worker intercepts it.

```bash
npm run build   # production build
npm run lint    # eslint
npx tsc --noEmit  # typecheck
```

## Before deploying

- **Set `NEXT_PUBLIC_SITE_URL`** (see `.env.example`). It feeds canonical URLs,
  `sitemap.xml`, `robots.txt` and Open Graph tags. Without it, crawlers are
  handed `localhost`.
- **Add MX records for `jutsu.co.in`, and make `hello@jutsu.co.in` a monitored
  mailbox.** The address is published on both legal pages, in the footer and by
  every "Contact us" CTA, and it is defined once as `CONTACT_EMAIL` in
  `lib/content.ts`. As of this writing the domain has **no MX records at all**, so
  the address bounces — which is the correct failure while it is unattended (a
  bounce tells the sender their message did not arrive) but is not a state to ship
  in. This is a DNS change only the domain's owner can make.

  It replaced `hello@jutsu.dev`, which was not a harmless placeholder: `jutsu.dev`
  is registered to a third party — Cloudflare-hosted, with live Protonmail MX —
  while this project's DNS is GoDaddy. Privacy and legal enquiries, which contain
  personal data by definition, were being addressed to a mailbox nobody here
  controls. `lib/content.test.ts` now fails if any published address leaves the
  owned domain.
- Review `app/privacy/page.tsx` and `app/terms/page.tsx` with someone qualified.
  They are written to be accurate about how the product works, not to be legal
  advice.

## Brand assets

Both are generated from source artwork by committed scripts, so neither needs to
be re-drawn by hand. Both are pure Node — no `sharp`, no native deps.

```bash
node scripts/trace-wordmark.js <source.png> <out.json>   # wordmark outlines
node scripts/make-icons.js                               # app/apple icons
```

- `assets/jutsu-logo-source.png` is the supplied logo at full resolution. It is
  **not** served — `public/jutsu-logo.png` is a 256px copy generated from it,
  because the mark never renders above 32px and the 1254px original was ~900KB
  of deploy weight.
- `lib/wordmark-paths.ts` holds the JUTSU lockup **vector-traced from the
  original raster artwork** — real letterforms, not a lookalike font. The page,
  the social card and the 404 all render from this one source.
- `app/icon.png` and `app/apple-icon.png` are derived from the logo. The Apple
  icon is composited onto a light plate because iOS discards transparency and
  the logo's black lobe would vanish on a dark one.

## Design system

Tokens live in `app/globals.css`. The one non-obvious rule:

> `--brand` and `--graph` **flip lightness role between themes** — bright green
> on obsidian, deep green on the off-white ground — and `--brand-foreground`
> inverts to match. That lets a single set of utilities (`text-brand`,
> `bg-brand`, `border-brand/40`) stay legible in both themes without per-theme
> overrides at every call site.

Palette green is sampled from the logo itself (`#83d005` / `#499f02`). Teal is
held at low chroma so green stays dominant.

**Re-check contrast in both themes after any palette edit.** Every text style on
the page currently clears WCAG AA.

## Structure

```
app/
  page.tsx              composition of the landing sections
  layout.tsx            metadata, fonts, JSON-LD, theme provider
  opengraph-image.tsx   social card, rendered from the traced lockup
  robots.ts sitemap.ts manifest.ts
  privacy/ terms/ not-found.tsx
components/site/        page sections and motion primitives
components/ui/          shadcn primitives
lib/content.ts          every user-facing string
scripts/                build-time asset generation
assets/                 full-resolution source art (not served)
```

All copy lives in `lib/content.ts` so the narrative can be edited without
touching layout, and so it ports to a CMS cleanly.

## Accessibility notes

Deliberate choices worth preserving:

- The hero memory graph is keyboard operable — each node is a real focusable
  control with an accessible name, and node **shape** encodes type so colour is
  never the only signal.
- Every scroll-driven animation collapses under `prefers-reduced-motion`, and
  the graph's rAF loop suspends when off-screen.
- FAQ and architecture panels stay mounted when collapsed, so `aria-controls`
  always resolves and the content stays in the crawled HTML.
- Counted-up statistics carry the real figure in an `sr-only` node, so a screen
  reader never announces a mid-count value.
