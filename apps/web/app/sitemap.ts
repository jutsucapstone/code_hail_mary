import type { MetadataRoute } from "next";

import { siteConfig } from "@/lib/content";

/**
 * Only what is actually indexable belongs here.
 *
 * `/pilot` and every `(product)` surface set `robots: { index: false }` on their own
 * layouts, so listing them would ask a crawler to fetch pages that tell it to go away —
 * which Search Console reports as an error against the sitemap rather than ignoring.
 * The two legal pages are indexable and are the only other public routes.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: siteConfig.url, changeFrequency: "weekly", priority: 1 },
    { url: `${siteConfig.url}/terms`, changeFrequency: "yearly", priority: 0.3 },
    { url: `${siteConfig.url}/privacy`, changeFrequency: "yearly", priority: 0.3 },
  ];
}
