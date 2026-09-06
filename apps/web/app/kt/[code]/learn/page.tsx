"use client";

import { LearningPath, WorkspaceRegion } from "@/components/kt/kt-workspace";
import { Skeleton } from "@/components/states";

/**
 * Learning path — the full, day-by-day form of the overview's summary.
 *
 * Every step is a claim or document the recipient's own account may read, ordered by the
 * backend from that evidence; marks are stored per recipient and re-read on every load.
 */
export default function Page() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h2 className="display text-xl font-semibold">Learning path</h2>
        <p className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Ordered from the evidence you can read: the most recent claims in each category,
          and the newest documents in the window. Mark what you have covered; mark what is
          still unclear and it appears on your overview.
        </p>
      </div>
      <WorkspaceRegion
        skeleton={
          <div className="flex flex-col gap-3">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-24" />
            ))}
          </div>
        }
      >
        <LearningPath compact={false} />
      </WorkspaceRegion>
    </div>
  );
}
