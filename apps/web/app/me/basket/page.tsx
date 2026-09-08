"use client";

import { KnowledgeBasket } from "@/components/basket/knowledge-basket";

/**
 * The Knowledge Basket — an employee's own files, uploaded by them.
 *
 * Filed under Knowledge rather than Account because that is what it changes: a file
 * added here becomes a document in the corpus, retrievable by Ask JUTSU under the
 * uploader's own ACL principal and nobody else's.
 *
 * **A knowledge-transfer package does not share these.** `kt_documents` resolves the
 * *recipient's* principals — "the package contributes the period; it grants nothing" —
 * so a basket file reaches a colleague only if their own access already reaches it,
 * which for a `basket:{owner}` grant it never does. The page says so plainly rather
 * than letting the section's name imply otherwise.
 *
 * The page is a shell. Everything that can fail — the ticket, the upload, the
 * extraction — belongs to the component, because the states it has to render are per
 * file, not per page.
 */
export default function KnowledgeBasketPage() {
  return (
    <div className="flex flex-col gap-8">
      <header>
        <p className="eyebrow flex items-center gap-2.5 text-brand">
          <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
          Knowledge basket
        </p>
        <h1 className="display mt-4 text-3xl font-semibold">Add what you know</h1>
        <p className="mt-3 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Not everything worth keeping lives in a connected application. Notes on a
          laptop, a recorded walkthrough, a spreadsheet nobody else has — put them here
          and they join your authorised context.
        </p>
        <p className="mt-3 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Search only ever returns these files to you — not to a colleague, and not to
          someone holding a knowledge-transfer package, because a package narrows what its
          holder can already read and never widens it. Your organisation&rsquo;s owner and
          IT administrators can list and download what is here, as they can for any
          company file.
        </p>
      </header>

      <KnowledgeBasket heading="Your files" />
    </div>
  );
}
