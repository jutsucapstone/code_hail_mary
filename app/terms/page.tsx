import type { Metadata } from "next";

import { LegalPage } from "@/components/site/legal-page";
import { siteConfig } from "@/lib/content";

export const metadata: Metadata = {
  title: "Terms",
  description: `The terms covering use of the ${siteConfig.name} website and pilot programme.`,
  alternates: { canonical: "/terms" },
};

export default function TermsPage() {
  return (
    <LegalPage
      title="Terms"
      updated="August 2026"
      intro={`These terms cover use of this website and participation in the ${siteConfig.name} private beta. A commercial deployment is governed by a separate signed agreement, which takes precedence over anything here.`}
    >
      <h2>Status of the product</h2>
      <p>
        {siteConfig.name} is in private beta. Features described on this site reflect the
        product as planned and may change. Nothing on this website is an offer, a warranty,
        or a commitment to deliver a specific capability on a specific date.
      </p>

      <h2>Using this website</h2>
      <p>
        You may read, link to and share this site freely. You may not scrape it at a rate
        that degrades it for others, misrepresent its content as your own, or use the
        {" "}
        {siteConfig.name} name or marks to imply a partnership that does not exist.
      </p>

      <h2>The private beta</h2>
      <p>
        Design partners are accepted at our discretion. A pilot is scoped, priced and
        governed by a separate written agreement covering data processing, security,
        support and termination. Until that agreement is signed, no obligations arise on
        either side.
      </p>

      <h2>Third-party names</h2>
      <p>
        Product names used for comparison on this site are trademarks of their respective
        owners. Comparisons reflect publicly documented capabilities at the date shown next
        to them. {siteConfig.name} is not affiliated with, endorsed by, or sponsored by any
        of them.
      </p>

      <h2>Liability</h2>
      <p>
        This website is provided as-is. To the extent permitted by law we exclude liability
        for loss arising from reliance on it. This does not limit liability that cannot
        lawfully be limited.
      </p>

      <h2>Contact</h2>
      <p>
        Questions about these terms: <a href="mailto:hello@jutsu.dev">hello@jutsu.dev</a>.
      </p>
    </LegalPage>
  );
}
