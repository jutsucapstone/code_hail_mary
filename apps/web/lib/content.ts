/**
 * Single source of truth for every string on the landing page.
 *
 * Copy is derived from the "Corporate Memory Graph — Enterprise Memory OS"
 * capstone deck (Team Code Hail Mary, Manipal University Jaipur, July 2026).
 * Keeping it here means the marketing narrative can be reviewed and edited
 * without touching layout code, and it stays trivially portable to a CMS.
 */

import { PILOT_PATH } from "@/lib/surfaces";

/**
 * The two destinations every call to action resolves to.
 *
 * "Request a pilot" opens the product; "Contact us" reaches a person. Naming them once
 * keeps the header, hero, contact section, announcement bar and footer from drifting
 * apart — the CTA that matters most appears in five places.
 */
const CONTACT_MAILTO = "mailto:hello@jutsu.dev?subject=JUTSU%20enquiry";

export const siteConfig = {
  name: "JUTSU",
  legalName: "Corporate Memory Graph",
  tagline: "Know. Connect. Remember.",
  description:
    "JUTSU is an Enterprise Memory OS. One living graph of your people, projects, decisions and skills — with AI agents that answer with citations, find real experts, score knowledge risk and auto-draft handovers.",
  /**
   * Absolute origin, used for canonical URLs, sitemap, robots and OG tags.
   * Set NEXT_PUBLIC_SITE_URL at build time; the fallback only serves local dev
   * and must not be what ships to production.
   */
  // `||`, not `??`. A Docker `ARG` that was never given a value arrives as an empty
  // string rather than as undefined, and `??` passes it straight through — so
  // `metadataBase: new URL("")` throws `ERR_INVALID_URL` and the whole build dies on
  // `/_not-found`, with nothing in the message naming the variable. An unset build
  // argument should behave like an unset variable.
  url: process.env.NEXT_PUBLIC_SITE_URL || "http://localhost:3210",
  org: {
    team: "Code Hail Mary",
    institution: "Manipal University Jaipur",
    program: "Capstone Program 2026",
    date: "July 2026",
  },
  team: [
    { name: "Anvesha Verma", role: "AI / RAG" },
    { name: "Ritik Raj", role: "Graph & Ingestion" },
    { name: "Yash Verma", role: "Platform & API" },
    { name: "Daksh Aryan", role: "Experience & Eval" },
  ],
} as const;

/** The cold open. This is the first thing a visitor reads. */
export const manifesto = {
  eyebrow: `${siteConfig.name} — ${siteConfig.tagline}`,
  line: "The gap is not storage — every file is saved. The gap is memory: who did what, why, and what changed.",
  footnote: "Keep scrolling.",
} as const;

// Root-relative `/#hash`, not bare `#hash`: the header and footer also render on
// /privacy and /terms, where a bare fragment resolves against the legal page and
// silently goes nowhere. Consumers that need the section id strip the `/#` prefix.
export const nav = [
  { label: "Problem", href: "/#problem" },
  { label: "How it works", href: "/#how-it-works" },
  { label: "Architecture", href: "/#architecture" },
  { label: "Comparison", href: "/#landscape" },
  { label: "FAQ", href: "/#faq" },
] as const;

/**
 * The returning-customer door.
 *
 * Called "Console" rather than "Sign in" because that is what it opens and what the
 * product calls it — an administrator lands on the admin console, a member on theirs.
 * "Sign in" describes the turnstile; the label people scan for is the room.
 */
export const consoleCta = {
  label: "Console",
  /** Used where there is room for a sentence — the mobile menu, not the header chip. */
  description: "Already have a JUTSU ID? Sign back in.",
} as const;

export const hero = {
  badge: "Private beta · Enterprise Memory OS",
  headline: siteConfig.name,
  tagline: siteConfig.tagline,
  subhead:
    "One living memory of your organisation — people, projects, decisions and skills in a single temporal graph. Ask anything, trace any decision, lose nothing.",
  // Opens the pilot funnel, where a visitor picks whether they are setting an
  // organisation up or joining one that already exists.
  primaryCta: { label: "Request a pilot", href: PILOT_PATH },
  // Primary opens the platform; this one reaches a human, so the two buttons do not
  // land in the same place.
  secondaryCta: {
    label: "Contact us",
    href: CONTACT_MAILTO,
  },
  kicker: "Ask anything. Trace any decision. Lose nothing.",
  stack: [
    "Next.js",
    "FastAPI",
    "LangGraph",
    "LangChain",
    "Neo4j",
    "PostgreSQL + pgvector",
    "Gemini",
    "Whisper",
    "Docker",
    "OAuth2 / SAML",
  ],
} as const;

/** The substrate the interactive hero graph is labelled with. */
export const memoryCore = {
  title: "Corporate Memory Graph",
  substrate: "Neo4j + pgvector, fed by LangChain",
  hint: "Hover or focus a node to trace its connections.",
} as const;

export const problem = {
  eyebrow: "The problem",
  title: "Companies forget.",
  lead: "Institutional knowledge is an asset with no ledger — it walks out the door unrecorded.",
  /**
   * Split into prefix / number / suffix so the figure can be counted up on
   * scroll rather than rendered as a dead string.
   */
  stats: [
    {
      id: "cost",
      prefix: "$",
      value: 31.5,
      decimals: 1,
      suffix: "B",
      unit: "/ yr",
      label: "lost by Fortune 500 firms to poor knowledge sharing",
      source: "IDC",
    },
    {
      id: "search",
      prefix: "",
      value: 19,
      decimals: 0,
      suffix: "%",
      unit: "of the work week",
      label: "spent just searching for information",
      source: "McKinsey",
    },
    {
      id: "bus-factor",
      prefix: "~",
      value: 42,
      decimals: 0,
      suffix: "%",
      unit: "of know-how",
      label: "role-critical knowledge unique to a single person",
      source: "Panopto",
    },
  ],
  points: [
    {
      id: "exits",
      title: "Every exit is an erasure",
      body: "Every resignation, re-org and project roll-off erases institutional memory that nobody wrote down.",
    },
    {
      id: "scatter",
      title: "Context lives everywhere, and nowhere",
      body: "The where, the why and the who live in emails, chats, decks and meeting audio — and in people's heads.",
    },
    {
      id: "onboarding",
      title: "Onboarding pays twice",
      body: "New joiners spend months re-learning what the organisation has already paid to learn once.",
    },
    {
      id: "blindspot",
      title: "You find out when it breaks",
      body: "When a key person resigns, nobody knows what only they knew — until something breaks.",
    },
  ],
} as const;

export const architecture = {
  eyebrow: "Architecture",
  title: "Under the hood.",
  lead: "A layered Memory OS: read-only connectors in, a temporal graph at the core, agents on top. Select a layer to inspect it.",
  layers: [
    {
      id: "sources",
      label: "Data sources",
      summary: "Read-only connectors into the systems you already run.",
      nodes: [
        { name: "Email & documents", detail: "M365, SharePoint, Drive" },
        { name: "Meetings & chats", detail: "Teams, Slack, transcripts" },
        { name: "Tickets, wikis & code", detail: "Jira, Confluence, GitHub" },
      ],
    },
    {
      id: "ingest",
      label: "Ingest & understand",
      summary: "Parse, mask, extract entities and relations, embed.",
      nodes: [
        { name: "Connectors & parsing", detail: "LangChain loaders" },
        { name: "PII masking & ACL capture", detail: "privacy by design" },
        { name: "Entity & relation extraction", detail: "Gemini" },
        { name: "Embedding generation", detail: "batch + incremental" },
      ],
    },
    {
      id: "memory",
      label: "Memory core",
      summary: "The temporal graph plus a semantic index over the same corpus.",
      nodes: [
        {
          name: "Neo4j knowledge graph",
          detail: "people · projects · decisions · meetings · skills · clients",
        },
        { name: "pgvector semantic index", detail: "PostgreSQL embeddings" },
      ],
    },
    {
      id: "agents",
      label: "Agent layer · LangGraph",
      summary: "Agents that read the memory, guard it and hand it over.",
      nodes: [
        { name: "GraphRAG Q&A agent", detail: "Cypher + vector fusion" },
        { name: "Historian & Handover agents", detail: "Gemini" },
        { name: "Knowledge-risk scoring", detail: "graph centrality" },
        { name: "Expert discovery", detail: "contribution-weighted ranking" },
      ],
    },
    {
      id: "experience",
      label: "Experience · Next.js",
      summary: "Six surfaces on one memory.",
      nodes: [
        { name: "Cited Q&A chat", detail: "grounded, traceable" },
        { name: "Expert finder", detail: "ranked humans" },
        { name: "Project historian", detail: "living timelines" },
        { name: "Onboarding copilot", detail: "personalised paths" },
        { name: "Risk dashboard", detail: "bus-factor view" },
        { name: "Handover studio", detail: "one-click packs" },
      ],
    },
  ],
  security: [
    "SSO (OAuth2 / SAML)",
    "RBAC with source-ACL inheritance",
    "AES-256 at rest · TLS 1.3",
    "PII masking",
    "Audit trail",
    "DPDP Act 2023 / GDPR aligned",
  ],
} as const;

export const landscape = {
  eyebrow: "Landscape",
  title: "Why the incumbents don't cover this.",
  lead: "Glean, Microsoft Viva, Guru and Atlassian Rovo index and search documents. None of them keep a temporal decision ledger, score bus-factor risk, or auto-draft cited handover packs.",
  moat: "Our moat is the graph of who-decided-what-when — not another index.",
  columns: ["JUTSU", "Glean", "Microsoft Viva", "Guru", "Atlassian Rovo"],
  rows: [
    { capability: "Enterprise document search", values: [true, true, true, true, true] },
    { capability: "Semantic Q&A with citations", values: [true, true, true, true, true] },
    { capability: "Temporal decision ledger", values: [true, false, false, false, false] },
    { capability: "Live bus-factor risk scoring", values: [true, false, false, false, false] },
    { capability: "Auto-drafted cited handover packs", values: [true, false, false, false, false] },
    {
      capability: "Contribution-scored expert discovery",
      values: [true, "partial", "partial", false, false],
    },
  ],
  footnote:
    "Comparison reflects publicly documented capabilities of each product as of July 2026. Product names are trademarks of their respective owners; JUTSU is not affiliated with or endorsed by them.",
} as const;

export const contact = {
  eyebrow: "Get started",
  title: "Bring your organisation's memory online.",
  lead: "A read-only overlay on the systems you already run. No process disruption, no migration, a pilot in twelve weeks.",
  // Also drives the header button, desktop and mobile (site-header.tsx).
  primaryCta: { label: "Request a pilot", href: PILOT_PATH },
  // The section's route to a human. Without it this block would have no way to reach
  // one, now that the primary opens the platform instead of a mail client.
  secondaryCta: { label: "Contact us", href: CONTACT_MAILTO },
} as const;

/** Thin bar above the header. Dismissed state persists per browser. */
export const announcement = {
  id: "private-beta-2026",
  label: "Private beta",
  message: "JUTSU is onboarding design partners.",
  cta: { label: "Request access", href: PILOT_PATH },
} as const;

export const howItWorks = {
  eyebrow: "How it works",
  title: "Three steps to organisational memory.",
  lead: "Read-only connectors in, a temporal graph in the middle, cited answers out. No migration, and no change to how anyone works.",
  steps: [
    {
      id: "connect",
      index: "01",
      title: "Connect your sources",
      body: "Point JUTSU at the systems you already run — mail, docs, chat, meetings, tickets. Connectors are read-only and inherit each source ACL, so nobody sees anything they could not already open.",
      detail: "Typical first sync: under a day.",
    },
    {
      id: "build",
      index: "02",
      title: "The graph builds itself",
      body: "Extraction turns that corpus into people, projects, decisions, meetings and skills — with the edges between them and the dates they happened. Nothing is written back.",
      detail: "Incremental from then on.",
    },
    {
      id: "ask",
      index: "03",
      title: "Ask, trace, hand over",
      body: "Query it in plain language and get answers cited to the source. Find who actually knows a topic, see where knowledge risk sits, and generate a leaver handover pack in one click.",
      detail: "Every claim carries a citation.",
    },
  ],
} as const;

export const faq = {
  eyebrow: "Questions",
  title: "What teams ask first.",
  items: [
    {
      q: "Does JUTSU write anything back to our systems?",
      a: "No. Every connector is read-only. JUTSU builds its own graph alongside your tools and never modifies a document, ticket, message or calendar entry.",
    },
    {
      q: "Can people see things they should not?",
      a: "No. Permissions are inherited from the source system at ingestion and enforced again at query time, so a result only surfaces for someone who could already open the underlying document. Access binds to your existing SSO and identity provider.",
    },
    {
      q: "How is this different from enterprise search?",
      a: "Search returns documents. JUTSU keeps a temporal graph of who decided what, when and why — so it answers questions no index can: who owns this, what did we already try, and what breaks if this person leaves.",
    },
    {
      q: "What happens to a departing colleague's knowledge?",
      a: "The knowledge-risk view scores bus-factor continuously, so exposure is visible before a resignation rather than after. When someone does leave, the Handover Generator drafts a cited pack of their open items, decisions, stakeholders and known gotchas.",
    },
    {
      q: "Where does our data live?",
      a: "In your own deployment. JUTSU is containerised and runs in your cloud or VPC, with encryption at rest and in transit, PII masking during ingestion, and a full audit trail. Aligned to the DPDP Act 2023 and GDPR.",
    },
    {
      q: "How long does a pilot take?",
      a: "Twelve weeks from first connector to an end-to-end deployment, scoped around a single department so the value is provable before it spreads.",
    },
  ],
} as const;

export const footerNav = [
  {
    heading: "Product",
    links: [
      { label: "The problem", href: "/#problem" },
      { label: "Memory graph", href: "/#hero" },
      { label: "How it works", href: "/#how-it-works" },
      { label: "Architecture", href: "/#architecture" },
      { label: "Comparison", href: "/#landscape" },
    ],
  },
  {
    heading: "Company",
    links: [
      { label: "Questions", href: "/#faq" },
      { label: "Request a pilot", href: PILOT_PATH },
      { label: "Contact us", href: CONTACT_MAILTO },
    ],
  },
  {
    heading: "Legal",
    links: [
      { label: "Privacy", href: "/privacy" },
      { label: "Terms", href: "/terms" },
      { label: "Security", href: "/#architecture" },
    ],
  },
] as const;

/**
 * The pilot funnel — the first screen behind "Request a pilot".
 *
 * Lives here with the rest of the copy rather than inline in the page, for the same
 * reason the marketing sections do: the wording of the two paths is a product decision
 * that gets edited far more often than the layout around it.
 *
 * Each card says who it is *for* before it says what it does. "Admin / HR" alone is a
 * job title; a visitor deciding between two doors needs to recognise themselves.
 */
export const pilot = {
  eyebrow: "Pilot access",
  title: "Choose how you're joining",
  lead: "JUTSU builds one living memory of your organisation. How you get in depends on whether you're setting it up or joining one that already exists.",
  paths: [
    {
      id: "admin",
      href: "/pilot/admin",
      icon: "org",
      label: "I'm setting up my organisation",
      role: "Admin, HR or IT",
      description:
        "Register your organisation, connect the tools it already runs on, and invite your people. You'll be the first administrator.",
      note: "Creates a new organisation",
    },
    {
      id: "employee",
      href: "/signin",
      icon: "employee",
      label: "I've been invited by my organisation",
      role: "Employee",
      description:
        "Sign in with the JUTSU ID your admin issued you, complete your profile, and connect your own accounts. You choose what to connect.",
      note: "Requires a JUTSU ID",
    },
  ],
  reassurance: [
    "Read-only. JUTSU never writes back to your tools.",
    "You connect each account yourself. Nothing is connected on your behalf.",
  ],
} as const;
