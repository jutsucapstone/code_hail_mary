/**
 * The six product surfaces (spec §3).
 *
 * One list drives the product nav, the stub pages and the middleware matcher, so a
 * surface cannot be routed without also being navigable — or vice versa.
 *
 * `status` is honest about what exists. §4.11 forbids mock data behind a UI surface, so
 * a stub says it is a stub rather than showing invented answers.
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
    status: "stub",
  },
  {
    slug: "decisions",
    name: "Decision Ledger",
    purpose:
      "Why did we choose PostgreSQL over MongoDB? The decision, its date, who decided, the meeting it happened in, and what superseded it.",
    kind: "differentiator",
    slice: "S20",
    status: "stub",
  },
  {
    slug: "experts",
    name: "Expert Discovery",
    purpose:
      "A topic in, ranked humans out — scored on demonstrated contribution rather than self-declared CV skills.",
    kind: "differentiator",
    slice: "S21",
    status: "stub",
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
    name: "Handover Studio",
    purpose:
      "One click to a cited leaver pack: open items, key decisions, stakeholders and gotchas, in under sixty seconds.",
    kind: "differentiator",
    slice: "S26–S27",
    status: "stub",
  },
  {
    slug: "onboarding",
    name: "Onboarding Copilot",
    purpose:
      "What should I read first for Project Falcon? An ordered reading path built from the graph.",
    kind: "table-stakes",
    slice: "S28",
    status: "stub",
  },
] as const;

export const surfaceBySlug = (slug: string): Surface | undefined =>
  SURFACES.find((s) => s.slug === slug);

/** Path prefixes the middleware treats as authenticated-only. */
export const PRODUCT_PATHS: readonly string[] = SURFACES.map((s) => `/${s.slug}`);
