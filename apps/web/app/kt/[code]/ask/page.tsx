"use client";

import { AskExperience } from "@/components/product/ask-experience";
import { useKtPackage } from "@/components/kt/kt-shell";

/**
 * Ask KT — plain-language search inside the workspace.
 *
 * The SAME retrieval endpoint as Ask JUTSU, under the recipient's own authorization —
 * deliberately not a package-scoped search path, because a second search route would be
 * a second place an ACL bug could live. The package frames the question; the caller's
 * grants bound the answer.
 */
export default function Page() {
  const { pkg } = useKtPackage();

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h2 className="display text-xl font-semibold">Knowledge Transfer Assistant</h2>
        <p className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Ask anything about {pkg.subject.display_name ?? "this package"}&apos;s context.
          Every passage returned is real evidence your account is authorised to read.
        </p>
      </div>
      <AskExperience />
    </div>
  );
}
