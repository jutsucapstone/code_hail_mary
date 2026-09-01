"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { BookOpenCheck } from "lucide-react";

import { Pill, StatStrip, When } from "@/components/admin/page-scaffold";
import {
  EmptyState,
  FailureState,
  LoadingRegion,
  Skeleton,
} from "@/components/states";
import { api } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * My Knowledge — what the caller's authorised context actually contains.
 *
 * Every number is an ACL-filtered count from `GET /v1/me/knowledge`, computed with the
 * same predicate retrieval uses, so this page cannot promise more than a search would
 * deliver. Zero is common and truthful — no linked source identity means no readable
 * documents — and the page explains it in exactly those terms rather than rendering a
 * blank that reads as a bug (§34).
 */
export default function MyKnowledgePage() {
  const knowledge = useQuery({
    queryKey: ["me", "knowledge"],
    queryFn: api.myKnowledge,
  });

  return (
    <div className="flex flex-col gap-8">
      <header>
        <p className="eyebrow flex items-center gap-2.5 text-brand">
          <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
          My knowledge
        </p>
        <h1 className="display mt-4 text-3xl font-semibold">Your authorised context</h1>
        <p className="mt-3 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          What your linked accounts make readable to you inside JUTSU. These are the
          documents Ask JUTSU answers from — nothing more, and nothing anyone else has
          that you do not.
        </p>
      </header>

      {knowledge.error ? (
        <FailureState
          failure={classifyApiError(knowledge.error)}
          onRetry={() => void knowledge.refetch()}
          deniedWhat="reading your knowledge summary"
        />
      ) : knowledge.isPending ? (
        <LoadingRegion label="Counting your authorised documents.">
          <div className="flex flex-col gap-3">
            <Skeleton className="h-28" />
            <Skeleton className="h-40" />
          </div>
        </LoadingRegion>
      ) : knowledge.data.total_documents === 0 ? (
        <EmptyState title="Nothing readable yet">
          <p>
            {knowledge.data.linked_identities === 0 ? (
              <>
                No source identity is linked to your account, so no documents are
                readable — access fails closed by design. Your administrator links
                identities from the console, and{" "}
                <Link href="/me/integrations" className="text-brand underline-offset-4 hover:underline">
                  connecting your own applications
                </Link>{" "}
                is how content reaches JUTSU in the first place.
              </>
            ) : (
              <>
                Your linked identities grant access to no ingested documents yet.
                Documents appear here as sources are ingested and their access lists
                include you.
              </>
            )}
          </p>
        </EmptyState>
      ) : (
        <>
          <StatStrip
            columns={2}
            stats={[
              {
                id: "docs",
                label: "Documents you can read",
                value: knowledge.data.total_documents,
                icon: BookOpenCheck,
              },
              {
                id: "identities",
                label: "Linked identities granting access",
                value: knowledge.data.linked_identities,
              },
            ]}
          />

          <section aria-labelledby="by-source-heading" className="flex flex-col gap-3">
            <h2 id="by-source-heading" className="display text-xl font-semibold">
              By source
            </h2>
            <ul className="flex flex-col gap-2">
              {knowledge.data.by_source.map((row) => (
                <li
                  key={row.source_system}
                  className="flex items-center justify-between gap-4 rounded-xl border border-hairline bg-surface/40 px-5 py-3"
                >
                  <span className="font-mono text-sm text-foreground">{row.source_system}</span>
                  <Pill tone="neutral">{row.documents} documents</Pill>
                </li>
              ))}
            </ul>
          </section>

          <section aria-labelledby="recent-heading" className="flex flex-col gap-3">
            <h2 id="recent-heading" className="display text-xl font-semibold">
              Most recent
            </h2>
            <ul className="flex flex-col gap-2">
              {knowledge.data.recent.map((doc) => (
                <li
                  key={doc.id}
                  className="flex items-center justify-between gap-4 rounded-xl border border-hairline bg-surface/40 px-5 py-3"
                >
                  <span className="min-w-0 truncate text-sm text-foreground">{doc.title}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    <When iso={doc.created_at} />
                  </span>
                </li>
              ))}
            </ul>
            <p className="max-w-prose text-pretty text-xs leading-relaxed text-muted-foreground">
              To read any of this, ask about it — <Link href="/ask" className="text-brand underline-offset-4 hover:underline">Ask JUTSU</Link>{" "}
              retrieves passages under exactly the access these counts describe.
            </p>
          </section>
        </>
      )}
    </div>
  );
}
