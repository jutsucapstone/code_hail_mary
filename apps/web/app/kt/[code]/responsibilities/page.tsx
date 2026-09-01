"use client";

import { KtInsightsList } from "@/components/kt/kt-insights";

export default function Page() {
  return (
    <KtInsightsList
      claimType="responsibility"
      title="Responsibilities"
      emptyWord="responsibilities"
    />
  );
}
