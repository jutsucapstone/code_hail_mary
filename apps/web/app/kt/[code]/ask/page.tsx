"use client";

import { useSearchParams } from "next/navigation";

import { KtCopilot } from "@/components/kt/kt-copilot";
import { useKtPackage } from "@/components/kt/kt-shell";

/**
 * Ask KT — the copilot inside the workspace.
 *
 * `POST /v1/kt/{code}/ask`: the same retrieval and the same grounding gate as Ask JUTSU,
 * narrowed to the package window, with the conversation so far as context. The package
 * frames the question; the caller's own grants bound the answer — and every conversation
 * is stored, so a recipient can leave and pick it up where they stopped.
 */
export default function Page() {
  const { pkg } = useKtPackage();
  // The overview's resume card links here with `?conversation={id}`. Nothing else in
  // the URL is read, and the id only selects — the API still decides what it shows.
  const params = useSearchParams();

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h2 className="display text-xl font-semibold">Ask KT</h2>
        <p className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Ask anything about {pkg.subject.display_name ?? "this package"}&apos;s context.
          The copilot answers from evidence inside this package&apos;s window that your
          account is authorised to read, with the conversation so far as context, and
          keeps every conversation so you can pick it up later.
        </p>
      </div>
      <KtCopilot initialConversationId={params.get("conversation")} />
    </div>
  );
}
