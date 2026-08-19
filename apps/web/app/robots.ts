import type { MetadataRoute } from "next";

import { siteConfig } from "@/lib/content";
import { PLATFORM_ENTRY_PATH } from "@/lib/surfaces";

export default function robots(): MetadataRoute.Robots {
  return {
    // `/enter` is a side-effecting endpoint — it mints a session and redirects — so it
    // is not content and crawlers have no business following it.
    //
    // The product routes are deliberately NOT disallowed: app/(product)/layout.tsx
    // already sets `index: false`, and blocking the crawl would stop a crawler ever
    // seeing that directive.
    rules: { userAgent: "*", allow: "/", disallow: PLATFORM_ENTRY_PATH },
    sitemap: `${siteConfig.url}/sitemap.xml`,
    host: siteConfig.url,
  };
}
