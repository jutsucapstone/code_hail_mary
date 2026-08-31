import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { QueryProvider } from "@/components/query-provider";
import { WordmarkGradientDefs } from "@/components/site/wordmark-art";
import { ThemeProvider } from "@/components/theme-provider";
import { Toaster } from "@/components/ui/sonner";
import { faq, siteConfig } from "@/lib/content";
import { MAIN_CONTENT_ID } from "@/lib/landmarks";
import { OPENING_SEEN_SCRIPT } from "@/lib/opening";

import "./globals.css";

const geistSans = Geist({
  variable: "--font-sans",
  subsets: ["latin"],
  display: "swap",
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  metadataBase: new URL(siteConfig.url),
  title: {
    default: `${siteConfig.name} — ${siteConfig.tagline}`,
    template: `%s · ${siteConfig.name}`,
  },
  description: siteConfig.description,
  applicationName: siteConfig.name,
  keywords: [
    "enterprise memory",
    "knowledge graph",
    "GraphRAG",
    "corporate memory graph",
    "knowledge management",
    "decision ledger",
    "bus factor",
    "employee offboarding",
    "handover automation",
    "expert discovery",
    "onboarding copilot",
    "Neo4j",
    "pgvector",
    "LangGraph",
  ],
  authors: siteConfig.team.map((member) => ({ name: member.name })),
  creator: siteConfig.org.team,
  publisher: siteConfig.org.institution,
  category: "technology",
  alternates: { canonical: "/" },
  openGraph: {
    type: "website",
    url: siteConfig.url,
    siteName: siteConfig.name,
    title: `${siteConfig.name} — ${siteConfig.tagline}`,
    description: siteConfig.description,
    locale: "en_US",
  },
  twitter: {
    card: "summary_large_image",
    title: `${siteConfig.name} — ${siteConfig.tagline}`,
    description: siteConfig.description,
  },
  robots: {
    index: true,
    follow: true,
    googleBot: { index: true, follow: true, "max-image-preview": "large" },
  },
  formatDetection: { telephone: false, address: false, email: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "light dark",
  // Matches --background in each theme, so browser UI blends with the page.
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f7f7f9" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0b0f" },
  ],
};

const structuredData = {
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "SoftwareApplication",
      name: siteConfig.name,
      alternateName: siteConfig.legalName,
      applicationCategory: "BusinessApplication",
      operatingSystem: "Web",
      description: siteConfig.description,
      url: siteConfig.url,
      offers: {
        "@type": "Offer",
        category: "Enterprise pilot",
        availability: "https://schema.org/PreOrder",
      },
    },
    {
      "@type": "FAQPage",
      mainEntity: faq.items.map((item) => ({
        "@type": "Question",
        name: item.q,
        acceptedAnswer: { "@type": "Answer", text: item.a },
      })),
    },
    {
      "@type": "Organization",
      name: siteConfig.org.team,
      parentOrganization: { "@type": "CollegeOrUniversity", name: siteConfig.org.institution },
      member: siteConfig.team.map((member) => ({ "@type": "Person", name: member.name })),
    },
  ],
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <body className="grain flex min-h-full flex-col bg-background text-foreground">
        {/* Runs synchronously, before anything below it is parsed, so the cold open is
            already hidden in the first paint for a returning visitor. Same technique
            next-themes uses to avoid a theme flash, and load-bearing for the same reason:
            deciding this in React would render the section and then remove it. */}
        <script dangerouslySetInnerHTML={{ __html: OPENING_SEEN_SCRIPT }} />
        <a
          href={`#${MAIN_CONTENT_ID}`}
          className="sr-only focus-visible:not-sr-only focus-visible:fixed focus-visible:left-4 focus-visible:top-4 focus-visible:z-100 focus-visible:rounded-md focus-visible:bg-brand focus-visible:px-4 focus-visible:py-2 focus-visible:text-sm focus-visible:font-medium focus-visible:text-brand-foreground"
        >
          Skip to main content
        </a>
        <WordmarkGradientDefs />
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          {/* Server-state cache for the consoles. Wrapping here rather than inside each
              console layout means one cache across both, so moving between /admin and
              /me does not re-ask who the caller is. `children` is passed through as a
              prop, so the marketing pages below stay server components. */}
          <QueryProvider>{children}</QueryProvider>
          {/* The toast channel. The primitive has existed since the console shipped and
              was never mounted, so `toast()` anywhere in the app was a silent no-op —
              which is worse than having no toasts at all, because a mutation looks like
              it reported success. Mounted once at the root: two Toasters render two of
              every notification. */}
          <Toaster position="bottom-right" closeButton />
        </ThemeProvider>
        <script
          type="application/ld+json"
          // Static, build-time constant — no user input reaches this string.
          dangerouslySetInnerHTML={{ __html: JSON.stringify(structuredData) }}
        />
      </body>
    </html>
  );
}
