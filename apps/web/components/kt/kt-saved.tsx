"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Pill, When } from "@/components/admin/page-scaffold";
import { EmptyState, LoadingRegion, Skeleton } from "@/components/states";
import { KtFailure } from "@/components/kt/kt-failure";
import { useKtPackage } from "@/components/kt/kt-shell";
import { api, type KtBookmark } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

/**
 * The recipient's saved items, from `GET /v1/kt/{code}/bookmarks`.
 *
 * Everything here is the recipient's own — a bookmark row is written by them and read
 * back to them, nobody else. What a bookmark *points at* is a different matter: the
 * server re-runs the recipient's ACL over the referent on every read and reports the
 * result as `available`. A claim whose evidence they can no longer read, or a document
 * that left the package window, stays in the list as a labelled stub rather than
 * vanishing, because a saved item that silently disappears reads as a bug.
 *
 * Questions are free text. They are the one kind the recipient authors here rather than
 * saving from another tab.
 */

/** The API's own bound on a bookmark note, mirrored so the field refuses before a round trip. */
const MAX_NOTE_CHARS = 2000;

const GROUPS: { kind: string; heading: string }[] = [
  { kind: "question", heading: "Questions" },
  { kind: "claim", heading: "Claims" },
  { kind: "document", heading: "Documents" },
  { kind: "message", heading: "Answers" },
];

function groupBookmarks(items: KtBookmark[]) {
  const known = new Set(GROUPS.map((group) => group.kind));
  const groups = GROUPS.map((group) => ({
    ...group,
    items: items.filter((item) => item.kind === group.kind),
  })).filter((group) => group.items.length > 0);
  // A kind this screen does not know is still something the recipient saved. It is
  // shown under its own name rather than dropped on the floor.
  const other = items.filter((item) => !known.has(item.kind));
  if (other.length > 0) {
    groups.push({ kind: "other", heading: "Other saved items", items: other });
  }
  return groups;
}

const ACTION_CLASS =
  "rounded-lg border border-hairline-strong px-3 py-1.5 text-xs font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60";

function SavedItem({
  item,
  code,
  onRemove,
  removing,
}: {
  item: KtBookmark;
  code: string;
  onRemove: () => void;
  removing: boolean;
}) {
  const openHref =
    item.available && item.tab
      ? `/kt/${encodeURIComponent(code)}/${encodeURIComponent(item.tab)}`
      : null;
  return (
    <li className="flex flex-col gap-3 rounded-xl border border-hairline bg-surface/40 p-5 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
      <div className="flex min-w-0 flex-col gap-1.5">
        <p className="text-pretty text-sm font-medium text-foreground">{item.label}</p>
        {item.note && item.kind !== "question" ? (
          <p className="text-pretty text-xs leading-relaxed text-muted-foreground">{item.note}</p>
        ) : null}
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <When iso={item.created_at} />
          {item.available === false ? (
            <Pill tone="neutral">No longer available to you</Pill>
          ) : null}
        </p>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {openHref ? (
          <Link href={openHref} aria-label={`Open: ${item.label}`} className={ACTION_CLASS}>
            Open
          </Link>
        ) : null}
        <button
          type="button"
          onClick={onRemove}
          disabled={removing}
          aria-label={`Remove saved item: ${item.label}`}
          className={ACTION_CLASS}
        >
          {removing ? "Removing…" : "Remove"}
        </button>
      </div>
    </li>
  );
}

export function KtSaved() {
  const { code } = useKtPackage();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");

  const bookmarks = useQuery({
    queryKey: ["kt", code, "bookmarks"],
    queryFn: () => api.ktBookmarks(code),
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["kt", code, "bookmarks"] });

  const saveQuestion = useMutation({
    mutationFn: (note: string) => api.ktBookmark(code, { kind: "question", note }),
    onSuccess: async () => {
      setDraft("");
      await invalidate();
      toast.success("Saved.");
    },
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  const remove = useMutation({
    mutationFn: (bookmarkId: string) => api.ktRemoveBookmark(code, bookmarkId),
    onSuccess: async () => {
      await invalidate();
      toast.success("Removed from your items.");
    },
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const note = draft.trim();
    if (!note || saveQuestion.isPending) return;
    saveQuestion.mutate(note);
  }

  return (
    <div className="flex flex-col gap-8">
      <form
        onSubmit={submit}
        className="flex flex-col gap-3 rounded-2xl border border-hairline bg-surface/40 p-5"
      >
        <label htmlFor="kt-saved-question" className="text-sm font-medium text-foreground">
          Save a question
        </label>
        <p className="text-pretty text-xs leading-relaxed text-muted-foreground">
          Something you want to raise with a colleague or check later. It is kept here, in
          this package, for you.
        </p>
        <div className="flex flex-col gap-3 sm:flex-row">
          <input
            id="kt-saved-question"
            type="text"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            maxLength={MAX_NOTE_CHARS}
            className="w-full flex-1 rounded-xl border border-hairline bg-background px-4 py-2.5 text-sm placeholder:text-muted-foreground/80 focus-visible:border-brand/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          />
          <button
            type="submit"
            disabled={saveQuestion.isPending || draft.trim().length === 0}
            className="shrink-0 rounded-xl border border-hairline-strong px-5 py-2.5 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50"
          >
            {saveQuestion.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </form>

      {bookmarks.error ? (
        <KtFailure
          failure={classifyApiError(bookmarks.error)}
          onRetry={() => void bookmarks.refetch()}
        />
      ) : bookmarks.isPending ? (
        <LoadingRegion label="Loading your saved items.">
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-16" />
            ))}
          </div>
        </LoadingRegion>
      ) : bookmarks.data.items.length === 0 ? (
        <EmptyState title="Nothing saved yet">
          <p>
            Save a claim or document from any tab, an answer from Ask KT, or a question you
            want to raise — they collect here.
          </p>
        </EmptyState>
      ) : (
        groupBookmarks(bookmarks.data.items).map((group) => (
          <section key={group.kind} aria-labelledby={`kt-saved-${group.kind}`} className="flex flex-col gap-3">
            <h3 id={`kt-saved-${group.kind}`} className="display text-lg font-semibold">
              {group.heading}
            </h3>
            <ul className="flex flex-col gap-2">
              {group.items.map((item) => (
                <SavedItem
                  key={item.id}
                  item={item}
                  code={code}
                  onRemove={() => remove.mutate(item.id)}
                  removing={remove.isPending && remove.variables === item.id}
                />
              ))}
            </ul>
          </section>
        ))
      )}
    </div>
  );
}
