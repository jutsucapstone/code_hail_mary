import type { Metadata } from "next";

import { LegalPage } from "@/components/site/legal-page";
import { siteConfig } from "@/lib/content";

export const metadata: Metadata = {
  title: "Privacy",
  description: `How ${siteConfig.name} handles personal data across the marketing site and an enterprise deployment.`,
  alternates: { canonical: "/privacy" },
};

export default function PrivacyPage() {
  return (
    <LegalPage
      title="Privacy"
      updated="August 2026"
      intro={`This page explains what ${siteConfig.name} does with personal data — both on this website and inside a customer deployment. It is written to be read, not to be survived.`}
    >
      <h2>This website</h2>
      <p>
        The marketing site sets no advertising or cross-site tracking cookies. A single
        entry in your browser&rsquo;s local storage records whether you dismissed the
        announcement bar and which colour theme you chose. Both stay on your device and
        are never transmitted.
      </p>
      <p>
        If you email us from a link on this site, we receive whatever you send — typically
        your name, address and the contents of your message — and use it only to reply.
      </p>

      <h2>Inside a deployment</h2>
      <p>
        {siteConfig.name} runs inside your own cloud or VPC. We do not host your corpus and
        we do not receive a copy of it. Within a deployment:
      </p>
      <ul>
        <li>Connectors are read-only. Nothing is written back to a source system.</li>
        <li>
          Permissions are inherited from each source at ingestion and enforced again at
          query time, so a result only surfaces for someone who could already open the
          underlying document.
        </li>
        <li>Personal identifiers are masked during ingestion.</li>
        <li>Data is encrypted in transit and at rest.</li>
        <li>Every query and every answer is written to an audit trail.</li>
      </ul>

      <h2>Knowledge-risk scoring</h2>
      <p>
        Bus-factor scores are reported in aggregate by default. Individual-level scoring is
        off unless a customer explicitly enables it, and enabling it is a decision for that
        customer to make with notice to the people affected. A data protection impact
        assessment is completed before any live pilot.
      </p>

      <h2>Your rights</h2>
      <p>
        Where the DPDP Act 2023 or the GDPR applies, you may request access to, correction
        of, or deletion of your personal data. For data held inside a customer deployment,
        that customer is the controller and we act on their instructions — contact them
        first, and we will support the request.
      </p>

      <h2>Contact</h2>
      <p>
        Questions about this policy: <a href="mailto:hello@jutsu.dev">hello@jutsu.dev</a>.
      </p>
    </LegalPage>
  );
}
