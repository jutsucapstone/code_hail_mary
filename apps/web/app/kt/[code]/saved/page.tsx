"use client";

import { KtSaved } from "@/components/kt/kt-saved";

export default function Page() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h2 className="display text-xl font-semibold">Saved</h2>
        <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          What you saved from this package: questions to ask, claims and documents to come
          back to, answers worth keeping. Only you see this.
        </p>
      </div>
      <KtSaved />
    </div>
  );
}
