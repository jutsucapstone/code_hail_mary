"use client";

import { FailureState } from "@/components/states";
import type { Failure } from "@/lib/api-error";

/**
 * A failed KT request, in the right words.
 *
 * Every recipient-facing KT route runs `_open_for` before anything else, so a 403 from
 * any of them is the PACKAGE's refusal — revoked or expired — carrying the exact sentence
 * to show (§39). The shared FailureState renders a 403 as "your role does not include…",
 * which is false here: no role grants or withholds these routes, and the shell's own
 * refusal (`KtRefusal`) already declines to say it. Every other failure — a spent budget,
 * a dependency down, a session that needs renewing — is FailureState's, unchanged.
 */
export function KtFailure({ failure, onRetry }: { failure: Failure; onRetry?: () => void }) {
  if (failure.kind === "denied") return <KtClosed failure={failure} />;
  return <FailureState failure={failure} onRetry={onRetry} />;
}

/**
 * The package's own refusal, as the server worded it. The title is not a heading: this
 * renders at several depths of the workspace, and a level chosen here would be wrong at
 * most of them.
 */
export function KtClosed({ failure }: { failure: Failure }) {
  return (
    <div
      role="alert"
      className="flex flex-col gap-2 rounded-2xl border border-hairline bg-surface/40 p-6"
    >
      <p className="display text-lg font-semibold text-foreground">This package is closed</p>
      <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
        {failure.message}
      </p>
    </div>
  );
}
