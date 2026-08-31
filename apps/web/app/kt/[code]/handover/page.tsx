"use client";

import Link from "next/link";

import { useKtPackage } from "@/components/kt/kt-shell";
import { When } from "@/components/admin/page-scaffold";

/**
 * Handover — what this package can hand over today, stated plainly.
 *
 * The generated executive pack (summary, open work, key contacts, risks) needs the
 * knowledge graph and an answer-composition layer; until those exist this page hands
 * over what is real — the scoped document window and the assistant — and says exactly
 * what is coming rather than generating a fake downloadable (§29).
 */
export default function Page() {
  const { pkg, code } = useKtPackage();
  const base = `/kt/${encodeURIComponent(code)}`;

  return (
    <div className="flex flex-col gap-6">
      <h2 className="display text-xl font-semibold">Handover</h2>
      <div className="flex flex-col gap-4 rounded-2xl border border-hairline bg-surface/40 p-8">
        <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          This package covers{" "}
          <strong className="text-foreground">
            {pkg.subject.display_name ?? "one colleague"}
          </strong>
          {pkg.subject.designation ? ` (${pkg.subject.designation})` : ""} and stays open
          until <When iso={pkg.expires_at} />. What you can act on now:
        </p>
        <ul className="flex flex-col gap-2 text-sm text-muted-foreground">
          <li>
            · Read the{" "}
            <Link className="text-brand underline-offset-4 hover:underline" href={`${base}/documents`}>
              documents
            </Link>{" "}
            in its window that your account is authorised to see.
          </li>
          <li>
            · Use{" "}
            <Link className="text-brand underline-offset-4 hover:underline" href={`${base}/ask`}>
              Ask KT
            </Link>{" "}
            to search that material in plain language.
          </li>
        </ul>
        <p className="max-w-prose text-pretty text-xs leading-relaxed text-muted-foreground">
          The generated executive handover pack — open work, key contacts, decisions and
          risks with citations — arrives with knowledge-graph extraction. It will be
          composed from real evidence, never generated as a placeholder document.
        </p>
      </div>
    </div>
  );
}
