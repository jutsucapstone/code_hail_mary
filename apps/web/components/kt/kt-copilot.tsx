"use client";

import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Bookmark, CircleSlash, FileText, Loader2, Search, Sparkles, X } from "lucide-react";
import { toast } from "sonner";

import { LoadMore, When } from "@/components/admin/page-scaffold";
import { EmptyState, LoadingRegion, Skeleton } from "@/components/states";
import { KtFailure } from "@/components/kt/kt-failure";
import { useKtPackage } from "@/components/kt/kt-shell";
import {
  api,
  type Evidence,
  type KtConversation,
  type KtConversationPage,
  type KtCopilotTurn,
  type KtMessage,
  type KtStoredCitation,
} from "@/lib/api";
import { classifyApiError, type Failure } from "@/lib/api-error";

/**
 * The KT copilot — a kept conversation over the evidence inside one package's window.
 *
 * Everything rendered here came from the backend: the answer text, the citation set
 * (validated server-side against the retrieved passages — this component never mints a
 * citation), and the conversations themselves, which the API stores. Unlike Ask JUTSU,
 * "history" here is real context: the server reads the conversation so far before it
 * answers, and every conversation is listed until the recipient archives it.
 *
 * **The ask body carries only `question` and `conversation_id`.** No `k`, no filter, no
 * tenant, no person, no model. The package contributes the window and the caller's own
 * grants bound what is read, both server-side; nothing a browser sends can widen either.
 */

/** The API's own limit on a question, mirrored so the field refuses before a round trip. */
const MAX_QUESTION_CHARS = 4000;
//: The search endpoint's own bound. Sharing the question's 4000 let a recipient
//: type 3800 characters the server refuses with a 422 they did not cause — the
//: control has to say what the contract is, not discover it afterwards.
const MAX_SEARCH_CHARS = 200;

const SUGGESTED = [
  "What should I understand first?",
  "What decisions matter most here?",
  "Who should I contact, and about what?",
  "What is still unfinished?",
] as const;

const REFUSAL =
  "The evidence you are authorised to read does not answer this. JUTSU refuses rather than guesses.";

/**
 * What the thread renders per message. Derived from the API's message shape rather
 * than written by hand, so a field the server renames is a compile error here.
 */
type Turn = Pick<KtMessage, "id" | "role" | "content" | "citations" | "insufficient_evidence">;

/**
 * Turns the copilot returned in this session, before the stored conversation has been
 * re-read. Accumulated, not replaced: a second answer arriving before the first re-read
 * completes must not make the first one blink out.
 */
interface LocalTurns {
  conversationId: string;
  turns: Turn[];
}

function turnsFrom(question: string, turn: KtCopilotTurn): Turn[] {
  return [
    {
      id: turn.question_message_id,
      role: "user",
      content: question,
      citations: [],
      insufficient_evidence: false,
    },
    {
      id: turn.answer_message_id,
      role: "assistant",
      content: turn.answer ?? "",
      citations: turn.citations,
      insufficient_evidence: turn.insufficient_evidence,
    },
  ];
}

/** The first line of a turn, for the accessible name of its button — a thread has one
 *  "save" control per message, and a screen reader needs to tell them apart. */
function excerpt(content: string): string {
  const line = content.trim().split("\n")[0] ?? "";
  return line.length > 80 ? `${line.slice(0, 80)}…` : line;
}

function conversationTitle(conversation: Pick<KtConversation, "title">): string {
  return conversation.title?.trim() || "Untitled conversation";
}

const ACTION_BUTTON =
  "inline-flex items-center gap-1.5 rounded-md font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-brand transition-colors hover:text-brand/80 disabled:text-muted-foreground/60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand";

const SECONDARY_BUTTON =
  "rounded-lg border border-hairline-strong px-3 py-1.5 text-xs font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand";

/**
 * One citation, with its source span available on request.
 *
 * The evidence text is rendered as-is and **never** sliced with `char_start`/`char_end`.
 * Those index the original document, and masking changes lengths, so applying them to
 * the masked text would highlight the wrong span — quietly, and convincingly. "View
 * source" fetches the pair that actually belong together.
 *
 * `available: false` means the chunk behind the citation is no longer readable by this
 * recipient — the document was superseded, or their access changed since the answer was
 * stored. The label stays so the answer's markers still resolve; the button goes,
 * because the fetch would answer 404.
 */
function Citation({ citation }: { citation: KtStoredCitation }) {
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const open = useCallback(async () => {
    if (evidence || loading) return;
    setLoading(true);
    setFailure(null);
    try {
      setEvidence(await api.evidence(citation.chunk_id));
    } catch (error) {
      setFailure(classifyApiError(error).message);
    } finally {
      setLoading(false);
    }
  }, [evidence, loading, citation.chunk_id]);

  const label = `${citation.document_title} (${citation.source_system})`;

  return (
    <li className="flex flex-col gap-2 text-xs text-muted-foreground">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-brand">[{citation.marker}]</span>
        <span className="min-w-0 break-words">{label}</span>
        {citation.available ? (
          <button
            type="button"
            onClick={() => void open()}
            disabled={loading || evidence !== null}
            aria-label={`View source for [${citation.marker}] ${citation.document_title}`}
            className={ACTION_BUTTON}
          >
            {loading ? (
              <Loader2 aria-hidden="true" className="h-3 w-3 animate-spin motion-reduce:animate-none" />
            ) : (
              <FileText aria-hidden="true" className="h-3 w-3" />
            )}
            {evidence ? "Source shown" : "View source"}
          </button>
        ) : (
          <span className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground/80">
            No longer available to you
          </span>
        )}
      </div>

      {failure ? (
        <p role="alert" className="text-xs text-muted-foreground">
          {failure}
        </p>
      ) : null}

      {evidence ? (
        <div className="rounded-xl border border-hairline bg-background/60 p-4">
          <p className="eyebrow text-muted-foreground/80">Source span</p>
          <p className="mt-2 whitespace-pre-wrap text-pretty text-sm leading-relaxed text-foreground">
            {evidence.text}
          </p>
          <p className="mt-3 font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground/80">
            {evidence.source_system} · chars {evidence.char_start}–{evidence.char_end}
          </p>
        </div>
      ) : null}
    </li>
  );
}

function UserTurn({
  turn,
  onSave,
  saving,
}: {
  turn: Turn;
  onSave: () => void;
  saving: boolean;
}) {
  return (
    <li className="flex flex-col gap-2">
      <p className="eyebrow text-muted-foreground/80">You asked</p>
      <p className="max-w-prose whitespace-pre-wrap text-pretty text-sm font-medium leading-relaxed text-foreground">
        {turn.content}
      </p>
      <button
        type="button"
        onClick={onSave}
        disabled={saving}
        aria-label={`Save as a question: ${excerpt(turn.content)}`}
        className={`${ACTION_BUTTON} self-start`}
      >
        <Bookmark aria-hidden="true" className="h-3 w-3" />
        {saving ? "Saving…" : "Save as a question"}
      </button>
    </li>
  );
}

function AssistantTurn({
  turn,
  onSave,
  saving,
}: {
  turn: Turn;
  onSave: () => void;
  saving: boolean;
}) {
  return (
    <li>
      <article className="flex flex-col gap-4 rounded-2xl border border-hairline bg-surface/40 p-6">
        {turn.insufficient_evidence ? (
          <div className="flex items-start gap-3">
            <span
              aria-hidden="true"
              className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-hairline-strong bg-surface text-muted-foreground"
            >
              <CircleSlash className="size-4" />
            </span>
            <p className="max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
              {REFUSAL}
            </p>
          </div>
        ) : (
          <>
            {/* The answer text carries its [n] markers inline; the list below is the
                same set resolved to documents. Both came from the server. */}
            <p className="max-w-prose whitespace-pre-wrap text-pretty text-sm leading-relaxed text-foreground">
              {turn.content}
            </p>
            {turn.citations.length > 0 ? (
              <div>
                <h4 className="font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground">
                  · Sources
                </h4>
                <ul className="mt-2 flex flex-col gap-2">
                  {turn.citations.map((citation) => (
                    <Citation key={`${citation.marker}-${citation.chunk_id}`} citation={citation} />
                  ))}
                </ul>
              </div>
            ) : null}
          </>
        )}
        <button
          type="button"
          onClick={onSave}
          disabled={saving}
          aria-label={`Save this answer: ${excerpt(turn.content)}`}
          className={`${ACTION_BUTTON} self-start`}
        >
          <Bookmark aria-hidden="true" className="h-3 w-3" />
          {saving ? "Saving…" : "Save this answer"}
        </button>
      </article>
    </li>
  );
}

/**
 * One row of the conversation list, with a two-step archive.
 *
 * Archiving hides a conversation from every list and search; it is not a delete, and
 * the API has no un-archive route, so the second click is the confirmation.
 */
function ConversationRow({
  conversation,
  selected,
  onSelect,
  onArchive,
  archiving,
}: {
  conversation: KtConversation;
  selected: boolean;
  onSelect: () => void;
  onArchive: () => void;
  archiving: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  const title = conversationTitle(conversation);
  const count = conversation.message_count;

  return (
    <li
      className={`flex flex-col gap-2 rounded-xl border px-4 py-3 ${
        selected ? "border-brand/40 bg-brand/5" : "border-hairline bg-surface/40"
      }`}
    >
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? "true" : undefined}
        aria-label={`Open conversation: ${title}`}
        className="flex min-w-0 flex-col items-start gap-1 rounded-md text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
      >
        <span className="w-full truncate text-sm text-foreground">{title}</span>
        <span className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
          <span>
            {count} {count === 1 ? "message" : "messages"}
          </span>
          <span aria-hidden="true">·</span>
          <When iso={conversation.updated_at} />
        </span>
      </button>

      {confirming ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">Archive this conversation?</span>
          <button
            type="button"
            onClick={onArchive}
            disabled={archiving}
            aria-label={`Confirm archive of ${title}`}
            className={SECONDARY_BUTTON}
          >
            {archiving ? "Archiving…" : "Confirm"}
          </button>
          <button
            type="button"
            onClick={() => setConfirming(false)}
            disabled={archiving}
            aria-label={`Cancel archiving ${title}`}
            className={SECONDARY_BUTTON}
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setConfirming(true)}
          aria-label={`Archive ${title}`}
          className={`${ACTION_BUTTON} self-start`}
        >
          <Archive aria-hidden="true" className="h-3 w-3" />
          Archive
        </button>
      )}
    </li>
  );
}

/**
 * `initialConversationId` is the conversation to open on arrival — the overview's resume
 * card links here with one. It seeds the selection and nothing else: the conversation is
 * still read from the API under the package's authorization like any other.
 */
export function KtCopilot({
  initialConversationId = null,
}: {
  initialConversationId?: string | null;
}) {
  const { code } = useKtPackage();
  const queryClient = useQueryClient();

  const [selectedId, setSelectedId] = useState<string | null>(initialConversationId);
  const [draft, setDraft] = useState("");
  const [lastQuestion, setLastQuestion] = useState<string | null>(null);
  const [local, setLocal] = useState<LocalTurns | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [notConfigured, setNotConfigured] = useState(false);

  // The conversation list, with older pages appended the way KtDocuments does it.
  const [older, setOlder] = useState<KtConversation[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [exhausted, setExhausted] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  // Search results replace the list until cleared. Kept apart from the list query so
  // clearing a search is a state change, not a refetch.
  const [searchDraft, setSearchDraft] = useState("");
  const [searchResults, setSearchResults] = useState<KtConversationPage | null>(null);

  const conversations = useQuery({
    queryKey: ["kt", code, "conversations"],
    queryFn: () => api.ktConversations(code),
  });

  const detail = useQuery({
    queryKey: ["kt", code, "conversation", selectedId],
    queryFn: () => api.ktConversation(code, selectedId as string),
    enabled: selectedId !== null,
  });

  const ask = useMutation({
    mutationFn: ({ question, conversationId }: { question: string; conversationId: string | null }) => {
      // The entire request. Nothing that names a tenant, a person, a filter or a model,
      // and no `k` — how much the copilot reads is the server's default to decide.
      return api.ktAsk(code, conversationId ? { question, conversation_id: conversationId } : { question });
    },
    onSuccess: (turn, { question }) => {
      setFailure(null);
      setLocal((current) =>
        current && current.conversationId === turn.conversation_id
          ? { ...current, turns: [...current.turns, ...turnsFrom(question, turn)] }
          : { conversationId: turn.conversation_id, turns: turnsFrom(question, turn) },
      );
      setSelectedId(turn.conversation_id);
      setDraft("");
      void queryClient.invalidateQueries({ queryKey: ["kt", code, "conversations"] });
      void queryClient.invalidateQueries({
        queryKey: ["kt", code, "conversation", turn.conversation_id],
      });
    },
    onError: (error: unknown) => {
      const classified = classifyApiError(error);
      if (classified.status === 503 && /not configured/i.test(classified.message)) {
        // The deployment has no answer provider. Say so once, in place — and do NOT
        // fall back to the corpus-wide evidence search: that searches outside this
        // package's window, which is not what the recipient asked for.
        setNotConfigured(true);
      } else {
        setFailure(classified);
      }
    },
  });

  const search = useMutation({
    // POST, not a query string: the words are the recipient's own, and a URL reaches
    // access logs, proxy logs and `Referer` headers that a request body does not.
    mutationFn: (q: string) => api.ktSearchConversations(code, q),
    onSuccess: (page) => setSearchResults(page),
    onError: (error: unknown) => toast.error(classifyApiError(error).message),
  });

  const archive = useMutation({
    mutationFn: (conversationId: string) => api.ktArchiveConversation(code, conversationId),
    onSuccess: (_, conversationId) => {
      if (selectedId === conversationId) {
        setSelectedId(null);
        setLocal(null);
      }
      setOlder((current) => current.filter((item) => item.id !== conversationId));
      setSearchResults((current) =>
        current
          ? { ...current, items: current.items.filter((item) => item.id !== conversationId) }
          : current,
      );
      void queryClient.invalidateQueries({ queryKey: ["kt", code, "conversations"] });
      toast.success("Conversation archived.");
    },
    onError: (error: unknown) => toast.error(classifyApiError(error).message),
  });

  const save = useMutation({
    mutationFn: (body: Parameters<typeof api.ktBookmark>[1]) => api.ktBookmark(code, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["kt", code, "bookmarks"] });
      toast.success("Saved to your items.");
    },
    onError: (error: unknown) => toast.error(classifyApiError(error).message),
  });

  function submit(question: string) {
    const trimmed = question.trim();
    if (!trimmed || ask.isPending) return;
    setFailure(null);
    setLastQuestion(trimmed);
    ask.mutate({ question: trimmed, conversationId: selectedId });
  }

  function startNew() {
    setSelectedId(null);
    setLocal(null);
    setFailure(null);
  }

  function select(conversationId: string) {
    setSelectedId(conversationId);
    setFailure(null);
    if (local && local.conversationId !== conversationId) setLocal(null);
  }

  async function loadOlder() {
    const next = cursor ?? conversations.data?.next_cursor;
    if (!next) return;
    setLoadingMore(true);
    try {
      const page = await api.ktConversations(code, { cursor: next });
      setOlder((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
      if (page.next_cursor === null) setExhausted(true);
    } catch (error) {
      toast.error(classifyApiError(error).message);
    } finally {
      setLoadingMore(false);
    }
  }

  const listed = conversations.data?.items ?? [];
  const seen = new Set(listed.map((item) => item.id));
  const rows: KtConversation[] =
    searchResults?.items ?? [...listed, ...older.filter((item) => !seen.has(item.id))];
  const more = searchResults === null && !exhausted && (cursor ?? conversations.data?.next_cursor);

  // Oldest first, and the stored conversation is the source of truth. Turns the copilot
  // returned in this session are overlaid only until the re-read conversation contains
  // them, so an answer renders at once and never twice.
  const stored: Turn[] = [...(detail.data?.messages ?? [])].sort((a, b) =>
    a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0,
  );
  const storedIds = new Set(stored.map((m) => m.id));
  const overlay =
    local && local.conversationId === selectedId
      ? local.turns.filter((turn) => !storedIds.has(turn.id))
      : [];
  const turns = [...stored, ...overlay];
  const pendingQuestion = ask.isPending ? ask.variables?.question : undefined;
  const showSuggestions = selectedId === null && !ask.isPending && !notConfigured;

  const saving = (body: { kind: string; ref_id?: string | null; note?: string | null }) =>
    save.isPending &&
    save.variables?.kind === body.kind &&
    (body.ref_id ? save.variables?.ref_id === body.ref_id : save.variables?.note === body.note);

  return (
    <div className="grid gap-6 lg:grid-cols-[18rem_minmax(0,1fr)]">
      <aside aria-labelledby="kt-copilot-conversations-heading" className="flex min-w-0 flex-col gap-4">
        <div className="flex items-center justify-between gap-3">
          <h3 id="kt-copilot-conversations-heading" className="display text-base font-semibold">
            Conversations
          </h3>
          <button type="button" onClick={startNew} className={SECONDARY_BUTTON}>
            New conversation
          </button>
        </div>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            const q = searchDraft.trim();
            if (q && !search.isPending) search.mutate(q);
          }}
          className="flex flex-col gap-2"
        >
          <label htmlFor="kt-copilot-search" className="sr-only">
            Search your conversations
          </label>
          <div className="flex gap-2">
            <div className="relative min-w-0 flex-1">
              <Search
                aria-hidden="true"
                className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
              />
              <input
                id="kt-copilot-search"
                type="search"
                value={searchDraft}
                onChange={(event) => setSearchDraft(event.target.value)}
                maxLength={MAX_SEARCH_CHARS}
                placeholder="Search what was said…"
                className="w-full rounded-lg border border-hairline bg-surface/40 py-2 pl-9 pr-3 text-sm placeholder:text-muted-foreground/80 focus-visible:border-brand/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
              />
            </div>
            <button
              type="submit"
              disabled={search.isPending || searchDraft.trim().length === 0}
              aria-busy={search.isPending}
              className={SECONDARY_BUTTON}
            >
              {search.isPending ? "Searching…" : "Search"}
            </button>
          </div>
          {searchResults ? (
            <button
              type="button"
              onClick={() => {
                setSearchResults(null);
                setSearchDraft("");
              }}
              className={`${ACTION_BUTTON} self-start`}
            >
              <X aria-hidden="true" className="h-3 w-3" />
              Clear search
            </button>
          ) : null}
        </form>

        {conversations.error ? (
          <KtFailure
            failure={classifyApiError(conversations.error)}
            onRetry={() => void conversations.refetch()}
          />
        ) : conversations.isPending ? (
          <LoadingRegion label="Loading your conversations.">
            <div className="flex flex-col gap-2">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-16" />
              ))}
            </div>
          </LoadingRegion>
        ) : rows.length === 0 ? (
          searchResults ? (
            <EmptyState title="No conversations match">
              <p>None of your conversations in this package say that. Clear the search to see them all.</p>
            </EmptyState>
          ) : (
            <EmptyState title="No conversations yet">
              <p>
                Ask anything about this package. Every conversation is kept here so you can
                pick it up later.
              </p>
            </EmptyState>
          )
        ) : (
          <>
            <ul className="flex flex-col gap-2">
              {rows.map((conversation) => (
                <ConversationRow
                  key={conversation.id}
                  conversation={conversation}
                  selected={conversation.id === selectedId}
                  onSelect={() => select(conversation.id)}
                  onArchive={() => archive.mutate(conversation.id)}
                  archiving={archive.isPending && archive.variables === conversation.id}
                />
              ))}
            </ul>
            {more ? <LoadMore onClick={() => void loadOlder()} pending={loadingMore} /> : null}
          </>
        )}
      </aside>

      <section aria-labelledby="kt-copilot-thread-heading" className="flex min-w-0 flex-col gap-6">
        <h3 id="kt-copilot-thread-heading" className="sr-only">
          Conversation
        </h3>

        {notConfigured ? (
          <div role="status" className="rounded-2xl border border-hairline bg-surface/40 p-5">
            <p className="eyebrow text-muted-foreground/80">Answers are not configured here</p>
            <p className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
              Answering in prose is not configured for this deployment, so the copilot
              cannot reply. The knowledge tabs — Documents, Projects, Decisions, People,
              Meetings and the rest — still work, and everything in them is real material
              inside this package&apos;s window that you are authorised to read.
            </p>
          </div>
        ) : (
          <>
            {selectedId !== null && detail.error ? (
              <KtFailure
                failure={classifyApiError(detail.error)}
                onRetry={() => void detail.refetch()}
              />
            ) : selectedId !== null && detail.isPending && turns.length === 0 ? (
              <LoadingRegion label="Loading the conversation.">
                <div className="flex flex-col gap-3">
                  <Skeleton className="h-6 w-2/3" />
                  <Skeleton className="h-28" />
                </div>
              </LoadingRegion>
            ) : null}

            {turns.length > 0 || pendingQuestion ? (
              <ol className="flex flex-col gap-5">
                {turns.map((turn) =>
                  turn.role === "user" ? (
                    <UserTurn
                      key={turn.id}
                      turn={turn}
                      onSave={() => save.mutate({ kind: "question", note: turn.content })}
                      saving={saving({ kind: "question", note: turn.content })}
                    />
                  ) : (
                    <AssistantTurn
                      key={turn.id}
                      turn={turn}
                      onSave={() => save.mutate({ kind: "message", ref_id: turn.id })}
                      saving={saving({ kind: "message", ref_id: turn.id })}
                    />
                  ),
                )}
                {pendingQuestion ? (
                  <li className="flex flex-col gap-2">
                    <p className="eyebrow text-muted-foreground/80">You asked</p>
                    <p className="max-w-prose whitespace-pre-wrap text-pretty text-sm font-medium leading-relaxed text-foreground">
                      {pendingQuestion}
                    </p>
                  </li>
                ) : null}
              </ol>
            ) : null}

            {ask.isPending ? (
              <LoadingRegion label="Composing an answer from the evidence in this package.">
                <Skeleton className="h-24" />
              </LoadingRegion>
            ) : null}

            {failure ? (
              // A 403 here is the package's own refusal — revoked or expired — in the
              // server's words; KtFailure renders it so. A spent budget answers the same
              // way until it resets, so a retry is offered for every other failure only.
              <KtFailure
                failure={failure}
                onRetry={
                  failure.kind !== "throttled" && lastQuestion ? () => submit(lastQuestion) : undefined
                }
              />
            ) : null}

            {showSuggestions ? (
              <div className="flex flex-wrap gap-2" role="group" aria-label="Suggested questions">
                {SUGGESTED.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    onClick={() => submit(suggestion)}
                    className="rounded-full border border-hairline-strong px-3.5 py-1.5 text-xs text-muted-foreground transition-colors hover:border-brand/40 hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            ) : null}

            <form
              onSubmit={(event) => {
                event.preventDefault();
                submit(draft);
              }}
              className="flex flex-col gap-3 sm:flex-row"
            >
              <label htmlFor="kt-copilot-question" className="sr-only">
                Your question
              </label>
              <input
                id="kt-copilot-question"
                type="text"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="Ask about this knowledge transfer…"
                maxLength={MAX_QUESTION_CHARS}
                className="h-12 min-w-0 flex-1 rounded-xl border border-hairline-strong bg-surface/40 px-4 text-sm text-foreground placeholder:text-muted-foreground/80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
              />
              <button
                type="submit"
                disabled={ask.isPending || !draft.trim()}
                aria-busy={ask.isPending}
                className="inline-flex h-12 items-center justify-center gap-2 rounded-xl bg-brand px-6 text-[0.9375rem] font-semibold text-brand-foreground transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60 sm:w-36"
              >
                {ask.isPending ? (
                  <Loader2 aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" />
                ) : (
                  <Sparkles aria-hidden="true" className="size-4" />
                )}
                {ask.isPending ? "Thinking…" : "Ask"}
              </button>
            </form>
          </>
        )}
      </section>
    </div>
  );
}
