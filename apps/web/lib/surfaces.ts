/**
 * The product surfaces (spec §3) the employee console routes today.
 *
 * One list drives the product nav, the stub pages and the middleware matcher, so a
 * surface cannot be routed without also being navigable — or vice versa.
 * `surfaces.test.ts` holds the route tree under `app/(product)/` to this list.
 *
 * `status` is honest about what exists. §4.11 forbids mock data behind a UI surface, so
 * a stub says it is a stub rather than showing invented answers.
 *
 * §3 names six surfaces, and three are deliberately absent until their slices land:
 * Decision Ledger (S20), Expert Discovery (S21) and Onboarding Copilot (S28). Each was a
 * "not built yet" page, and an entry here is a nav item and a gated route, so bringing
 * one back means adding its page in the same change.
 */

export type SurfaceStatus = "stub" | "live";

export interface Surface {
  /** URL segment under (product), and the middleware match prefix. */
  slug: string;
  name: string;
  /** What it must actually do, per §3. */
  purpose: string;
  /** Differentiators are the moat; table stakes are table stakes (§3). */
  kind: "differentiator" | "table-stakes";
  /** The slice that makes it live — see docs/plan-phase-1.md and §21. */
  slice: string;
  status: SurfaceStatus;
}

export const SURFACES: readonly Surface[] = [
  {
    slug: "ask",
    name: "Cited Q&A",
    purpose:
      "Ask in plain language, get a grounded answer where every claim is clickable through to a highlighted source span. Refuses rather than guesses.",
    kind: "table-stakes",
    slice: "S18–S19",
    status: "live",
  },
  {
    slug: "risk",
    name: "Knowledge Risk",
    purpose:
      "Live bus-factor per project and topic, showing where knowledge concentrates in a single head. Aggregate first.",
    kind: "differentiator",
    slice: "S24–S25",
    status: "stub",
  },
  {
    slug: "handover",
    // Named for what the page does. "Handover Studio" promised a one-click cited
    // leaver pack; what is live is the entry to a knowledge-transfer package — a
    // scoped, cited workspace with a copilot, a learning path and the recipient's own
    // saved items — plus an on-demand executive summary inside it. The generator the
    // old name described does not exist, and §4.11 does not let a label pretend it does.
    name: "Knowledge Transfer",
    purpose:
      "Open a knowledge-transfer package with its KT ID: a scoped, cited workspace over one colleague's context, with a copilot that answers from its evidence and a learning path built from it.",
    kind: "differentiator",
    slice: "S26–S28",
    status: "live",
  },
] as const;

export const surfaceBySlug = (slug: string): Surface | undefined =>
  SURFACES.find((s) => s.slug === slug);

/** Path prefixes the middleware treats as authenticated-only. */
export const PRODUCT_PATHS: readonly string[] = SURFACES.map((s) => `/${s.slug}`);

/**
 * The door into the product from the marketing site.
 *
 * A real page, so a `<Link>` is correct here — unlike the `/enter` route it replaced,
 * which was a Route Handler that minted a session cookie during a redirect and therefore
 * had to be a plain anchor. That route is gone: it issued a session with no identity,
 * which becomes an authentication bypass the moment real auth exists.
 */
export const PILOT_PATH = "/pilot";

/**
 * The way back in for somebody who already has an account.
 *
 * Deliberately not under `/pilot`. That subtree is the chooser and the two joining
 * flows — "setting up" or "been invited" — and a returning administrator whose session
 * expired is neither. They were sent to a page asking which kind of newcomer they were,
 * having been a customer for a month.
 *
 * Role-neutral, because the destination is not this page's decision: the API returns it
 * from `destination_for(role)`, so one form serves an owner, an admin and a member and
 * cannot disagree with the server about where any of them belongs.
 */
export const SIGN_IN_PATH = "/signin";

/** The surface a signed-in caller lands on when none is specified. */
export const DEFAULT_SURFACE = `/${SURFACES[0].slug}`;
