/**
 * Single source of truth for every string on the landing page.
 *
 * Copy is derived from the "Corporate Memory Graph — Enterprise Memory OS"
 * capstone deck (Team Code Hail Mary, Manipal University Jaipur, July 2026).
 * Keeping it here means the marketing narrative can be reviewed and edited
 * without touching layout code, and it stays trivially portable to a CMS.
 */

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
  url: process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3210",
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

export const nav = [
  { label: "Problem", href: "#problem" },
  { label: "How it works", href: "#how-it-works" },
  { label: "Architecture", href: "#architecture" },
  { label: "Comparison", href: "#landscape" },
  { label: "FAQ", href: "#faq" },
] as const;

export const hero = {
  badge: "Private beta · Enterprise Memory OS",
  headline: siteConfig.name,
  tagline: siteConfig.tagline,
  subhead:
    "One living memory of your organization — people, projects, decisions and skills in a single temporal graph. Ask anything, trace any decision, lose nothing.",
  primaryCta: { label: "Request a pilot", href: "#contact" },
  // Primary scrolls to the CTA section; this one is the direct action, so the
  // two buttons do not both land in the same place.
  secondaryCta: {
    label: "Contact us",
    href: "mailto:hello@jutsu.dev?subject=JUTSU%20enquiry",
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
      body: "New joiners spend months re-learning what the organization has already paid to learn once.",
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
  title: "Bring your organization's memory online.",
  lead: "A read-only overlay on the systems you already run. No process disruption, no migration, a pilot in twelve weeks.",
  primaryCta: {
    label: "Request a pilot",
    href: "mailto:hello@jutsu.dev?subject=JUTSU%20pilot%20enquiry",
  },
  secondaryCta: { label: "See the comparison", href: "#landscape" },
} as const;

/** Thin bar above the header. Dismissed state persists per browser. */
export const announcement = {
  id: "private-beta-2026",
  label: "Private beta",
  message: "JUTSU is onboarding design partners.",
  cta: { label: "Request access", href: "#contact" },
} as const;

export const howItWorks = {
  eyebrow: "How it works",
  title: "Three steps to organizational memory.",
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
      { label: "The problem", href: "#problem" },
      { label: "Memory graph", href: "#hero" },
      { label: "How it works", href: "#how-it-works" },
      { label: "Architecture", href: "#architecture" },
      { label: "Comparison", href: "#landscape" },
    ],
  },
  {
    heading: "Company",
    links: [
      { label: "Questions", href: "#faq" },
      { label: "Request a pilot", href: "#contact" },
      { label: "Contact", href: "mailto:hello@jutsu.dev" },
    ],
  },
  {
    heading: "Legal",
    links: [
      { label: "Privacy", href: "/privacy" },
      { label: "Terms", href: "/terms" },
      { label: "Security", href: "#architecture" },
    ],
  },
] as const;
