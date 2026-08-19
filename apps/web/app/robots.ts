import type { MetadataRoute } from "next";

import { siteConfig } from "@/lib/content";

export default function robots(): MetadataRoute.Robots {
  return {
    // Nothing is disallowed. The pilot funnel and the product surfaces are kept out of
    // the index by `robots: { index: false }` on their own layouts, which is the correct
    // mechanism — blocking the crawl instead would stop a crawler ever seeing it.
    rules: { userAgent: "*", allow: "/" },
    sitemap: `${siteConfig.url}/sitemap.xml`,
    host: siteConfig.url,
  };
}
