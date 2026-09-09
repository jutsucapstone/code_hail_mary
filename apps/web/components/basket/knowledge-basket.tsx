"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  Download,
  FileText,
  Loader2,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { Pill, When } from "@/components/admin/page-scaffold";
import { EmptyState, FailureState, LoadingRegion, Skeleton } from "@/components/states";
import { Button } from "@/components/ui/button";
import { api, type BasketFile } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import { cn } from "@/lib/utils";

/**
 * The Knowledge Basket: an employee's own files, and what actually happened to each one.
 *
 * **Bytes never pass through the API.** The browser asks for a signed URL, PUTs straight
 * to Cloud Storage, then tells the server the object has landed. That is not an
 * optimisation — a proxied upload would hit Cloud Run's 32 MiB request cap, so a
 * recording could not be uploaded at all.
 *
 * **Every state rendered here is the server's, and none is invented.** `ready` means the
 * text is in the corpus; `stored` means the file is kept and downloadable but cannot be
 * searched; `rejected` means the bytes were not what they claimed; `failed` means
 * extraction broke. The list says which, because the alternative — a spinner over a file
 * this deployment has no way to read — is the "pretend unsupported formats work" that
 * §4.11 forbids.
 */

/** How each server state reads, and whether it is still moving. */
const STATES: Record<
  string,
  { label: string; tone: "good" | "attention" | "bad" | "neutral"; working: boolean }
> = {
  uploading: { label: "Uploading", tone: "neutral", working: true },
  uploaded: { label: "Queued", tone: "attention", working: true },
  validating: { label: "Checking", tone: "attention", working: true },
  extracting: { label: "Reading", tone: "attention", working: true },
  chunking: { label: "Indexing", tone: "attention", working: true },
  embedding: { label: "Indexing", tone: "attention", working: true },
  ready: { label: "Searchable", tone: "good", working: false },
  stored: { label: "Stored", tone: "neutral", working: false },
  rejected: { label: "Not accepted", tone: "bad", working: false },
  failed: { label: "Could not read", tone: "bad", working: false },
  quarantined: { label: "Blocked", tone: "bad", working: false },
};

/**
 * How one row reads.
 *
 * The fallback matters: `basket_files.state` is constrained in the database, so an
 * unknown value here means this build is older than the API. Showing the raw value
 * without a spinner is the honest reading — it says something is there and does not
 * claim it is still working.
 */
function shownState(state: string) {
  return STATES[state] ?? { label: state, tone: "neutral" as const, working: false };
}

/** Mirrors `MAX_UPLOAD_BYTES`. Refused here so a mistake costs no upload. */
const MAX_BYTES = 512 * 1024 * 1024;

/**
 * Extension → content type, because a browser's `File.type` is not reliable.
 *
 * Windows reports `.csv` as `application/vnd.ms-excel` and reports `""` for anything it
 * has no handler registered for. The server pins the signed URL to whatever is declared
 * here and then reads the bytes anyway, so a wrong guess is caught rather than trusted —
 * but a guess that is right avoids refusing a file the product supports.
 */
const BY_EXTENSION: Record<string, string> = {
  txt: "text/plain",
  md: "text/markdown",
  csv: "text/csv",
  tsv: "text/tab-separated-values",
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  mp4: "video/mp4",
  webm: "video/webm",
  mov: "video/quicktime",
  mp3: "audio/mpeg",
  wav: "audio/wav",
  m4a: "audio/mp4",
  zip: "application/zip",
  // Pre-2007 Office. Stored and downloadable, never read for text — the four spellings
  // all carry the same sentence, and the API refuses to pretend otherwise.
  doc: "application/msword",
  ppt: "application/vnd.ms-powerpoint",
  xls: "application/vnd.ms-excel",
};

const ACCEPT = Object.keys(BY_EXTENSION)
  .map((extension) => `.${extension}`)
  .join(",");

function contentTypeFor(file: File): string {
  const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
  return BY_EXTENSION[extension] || file.type || "application/octet-stream";
}

export function readableSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** One upload the browser is doing right now, before the server has a state for it. */
interface InFlight {
  key: string;
  filename: string;
  size: number;
  /** 0–100, from the request's own progress events. Never an animation. */
  percent: number;
  failure?: string;
}

/**
 * A counter, so two uploads never share a React key.
 *
 * Not `Date.now()`: two files chosen in one picker interaction start in the same
 * millisecond, and a key collision renders one row for two uploads with the second
 * one's progress overwriting the first's.
 */
let uploads = 0;

/**
 * PUT the bytes to Cloud Storage, reporting real progress.
 *
 * `XMLHttpRequest` rather than `fetch`, and that is the entire reason it is here: `fetch`
 * has no upload progress event, so a 400 MB file would show an unmoving spinner for
 * several minutes with no way to tell it apart from a hang.
 */
function put(
  url: string,
  headers: Record<string, string>,
  file: File,
  onProgress: (percent: number) => void,
  signal: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", url);
    for (const [name, value] of Object.entries(headers)) {
      request.setRequestHeader(name, value);
    }
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    // Deliberately not the response body on failure: Cloud Storage answers with an XML
    // document naming the bucket and the object, which is an internal locator.
    request.onload = () =>
      request.status >= 200 && request.status < 300
        ? resolve()
        : reject(new Error(`Storage refused that upload (${request.status}).`));
    request.onerror = () => reject(new Error("That upload could not reach storage."));
    request.onabort = () => reject(new DOMException("aborted", "AbortError"));
    signal.addEventListener("abort", () => request.abort(), { once: true });
    request.send(file);
  });
}

export function KnowledgeBasket({ heading = "Your files" }: { heading?: string }) {
  const searchId = useId();
  const fileId = useId();
  const queryClient = useQueryClient();

  const [typed, setTyped] = useState("");
  const [search, setSearch] = useState("");
  const [dragging, setDragging] = useState(false);
  const [inFlight, setInFlight] = useState<InFlight[]>([]);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const aborts = useRef(new Map<string, AbortController>());

  // Debounced, so typing eight characters is one request rather than eight.
  useEffect(() => {
    const timer = setTimeout(() => setSearch(typed.trim()), typed ? 250 : 0);
    return () => clearTimeout(timer);
  }, [typed]);

  const files = useQuery({
    queryKey: ["basket", "files", search],
    queryFn: () => api.basketFiles({ q: search || null }),
    /**
     * Poll only while something is genuinely moving.
     *
     * Extraction is durable work on a queue, so a row changes without the browser doing
     * anything — but a fixed interval would be a permanent timer on a page whose files
     * all finished days ago. Reading the answer out of the data is what bounds it.
     */
    refetchInterval: (query) =>
      query.state.data?.items.some((file) => shownState(file.state).working) ? 3000 : false,
  });

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["basket"] });
  }, [queryClient]);

  const uploadOne = useCallback(
    async (file: File) => {
      uploads += 1;
      const key = `${file.name}:${file.size}:${uploads}`;
      const controller = new AbortController();
      aborts.current.set(key, controller);
      setInFlight((current) => [
        ...current,
        { key, filename: file.name, size: file.size, percent: 0 },
      ]);

      try {
        if (file.size === 0) throw new Error("That file is empty.");
        if (file.size > MAX_BYTES) throw new Error("That file is larger than 512 MB.");

        const ticket = await api.startBasketUpload({
          filename: file.name,
          content_type: contentTypeFor(file),
          size_bytes: file.size,
        });

        await put(
          ticket.url,
          ticket.headers,
          file,
          (percent) =>
            setInFlight((current) =>
              current.map((item) => (item.key === key ? { ...item, percent } : item)),
            ),
          controller.signal,
        );

        // The row's real state comes back from here — searchable, stored, or refused.
        // Until this returns, nothing has been claimed about the file.
        await api.completeBasketUpload(ticket.file_id);
        setInFlight((current) => current.filter((item) => item.key !== key));
        refresh();
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          setInFlight((current) => current.filter((item) => item.key !== key));
          return;
        }
        // The two local refusals (empty, too large) are plain `Error`s and already read
        // as sentences; anything from the API goes through the classifier.
        const message =
          error instanceof Error && !("status" in error)
            ? error.message
            : classifyApiError(error).message;
        setInFlight((current) =>
          current.map((item) => (item.key === key ? { ...item, failure: message } : item)),
        );
      } finally {
        aborts.current.delete(key);
      }
    },
    [refresh],
  );

  const addFiles = useCallback(
    (list: FileList | null) => {
      if (!list || list.length === 0) return;
      // One at a time rather than all at once. A browser opens six connections per
      // origin, so ten parallel uploads each crawl and none finishes; sequential means
      // the first file is done — and searchable — while the rest are still going.
      void [...list].reduce(
        (chain, file) => chain.then(() => uploadOne(file)),
        Promise.resolve(),
      );
    },
    [uploadOne],
  );

  const rename = useMutation({
    mutationFn: ({ id, filename }: { id: string; filename: string }) =>
      api.renameBasketFile(id, filename),
    onSuccess: () => {
      setRenaming(null);
      refresh();
    },
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  const retry = useMutation({
    mutationFn: (id: string) => api.retryBasketFile(id),
    onSuccess: () => {
      toast.success("Trying that file again.");
      refresh();
    },
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteBasketFile(id),
    onSuccess: () => {
      setConfirming(null);
      toast.success("That file was removed.");
      refresh();
    },
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  const download = useMutation({
    mutationFn: (id: string) => api.basketDownloadUrl(id),
    // A signed URL, minted per request after an authorization check and short-lived.
    // Assigning rather than opening a tab: a popup blocker eats `window.open` called
    // after an await, and the navigation replaces nothing because the signed response
    // carries `Content-Disposition: attachment`.
    onSuccess: ({ url }) => window.location.assign(url),
    onError: (error) => toast.error(classifyApiError(error).message),
  });

  const rows = files.data?.items ?? [];
  const pending = (id: string) =>
    (remove.isPending && remove.variables === id) ||
    (retry.isPending && retry.variables === id) ||
    (download.isPending && download.variables === id) ||
    (rename.isPending && rename.variables?.id === id);

  return (
    <section className="flex flex-col gap-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="display text-xl font-semibold sm:text-2xl">{heading}</h2>
          <p className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
            Documents, spreadsheets and slides become searchable. Images, audio, video and
            archives are kept and downloadable but not searched — the list says which is
            which, file by file.
          </p>
        </div>
        <div className="relative">
          <label htmlFor={searchId} className="sr-only">
            Search your files
          </label>
          <Search
            aria-hidden="true"
            className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
          />
          <input
            id={searchId}
            type="search"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            placeholder="Search files"
            className="h-11 w-full rounded-xl border border-hairline-strong bg-surface/40 pl-9 pr-3.5 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand sm:w-64"
          />
        </div>
      </header>

      {/* The drop zone. The input sits before the label so `peer-focus-visible` can style
          it — Tailwind's peer variant is a following-sibling combinator, and a visually
          hidden input with no focus ring is a keyboard trap in practice. */}
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          addFiles(event.dataTransfer.files);
        }}
        className={cn(
          "rounded-2xl border border-dashed px-6 py-8 text-center transition-colors",
          dragging ? "border-brand bg-brand/5" : "border-hairline-strong bg-surface/20",
        )}
      >
        <input
          id={fileId}
          type="file"
          multiple
          accept={ACCEPT}
          className="peer sr-only"
          onChange={(event) => {
            addFiles(event.target.files);
            // Cleared so choosing the same file twice fires `change` the second time.
            event.target.value = "";
          }}
        />
        <label
          htmlFor={fileId}
          className="inline-flex cursor-pointer flex-col items-center gap-2 rounded-lg text-sm text-muted-foreground peer-focus-visible:outline-2 peer-focus-visible:outline-offset-4 peer-focus-visible:outline-brand"
        >
          <Upload aria-hidden="true" className="size-5" />
          <span>
            Drop files here, or{" "}
            <span className="font-medium text-brand underline underline-offset-4">
              choose them
            </span>
            .
          </span>
          <span className="text-xs">Up to 512 MB each.</span>
        </label>
      </div>

      {inFlight.length > 0 ? (
        <ul aria-label="Uploads in progress" className="flex flex-col gap-2">
          {inFlight.map((item) => (
            <li
              key={item.key}
              className="flex flex-wrap items-center gap-3 rounded-xl border border-hairline bg-surface/40 px-4 py-3"
            >
              <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                {item.filename}
              </span>
              <span className="text-xs tabular-nums text-muted-foreground">
                {readableSize(item.size)}
              </span>
              {item.failure ? (
                <>
                  <span role="alert" className="text-xs text-destructive">
                    {item.failure}
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      setInFlight((current) =>
                        current.filter((entry) => entry.key !== item.key),
                      )
                    }
                  >
                    Dismiss
                  </Button>
                </>
              ) : (
                <>
                  <progress
                    value={item.percent}
                    max={100}
                    aria-label={`Uploading ${item.filename}`}
                    className="h-1.5 w-32 overflow-hidden rounded-full"
                  />
                  <span className="w-10 text-right font-mono text-xs tabular-nums text-muted-foreground">
                    {item.percent}%
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`Cancel uploading ${item.filename}`}
                    onClick={() => aborts.current.get(item.key)?.abort()}
                  >
                    Cancel
                  </Button>
                </>
              )}
            </li>
          ))}
        </ul>
      ) : null}

      {files.error ? (
        <FailureState
          failure={classifyApiError(files.error)}
          onRetry={() => void files.refetch()}
          deniedWhat="reading your Knowledge Basket"
        />
      ) : files.isPending ? (
        <LoadingRegion label="Loading your files.">
          <div className="flex flex-col gap-2">
            <Skeleton className="h-14" />
            <Skeleton className="h-14" />
            <Skeleton className="h-14" />
          </div>
        </LoadingRegion>
      ) : rows.length === 0 ? (
        <EmptyState title={search ? "No files match that" : "Nothing here yet"}>
          <p>
            {search
              ? "Try a different word, or clear the search to see everything."
              : "Add the notes, decisions, runbooks and recordings you would otherwise keep on your own laptop. They become part of what Ask JUTSU answers for you."}
          </p>
        </EmptyState>
      ) : (
        <ul className="flex flex-col gap-2">
          {rows.map((file) => (
            <li
              key={file.id}
              className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-hairline bg-surface/40 px-4 py-3"
            >
              <FileText aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />

              {renaming === file.id ? (
                <RenameForm
                  file={file}
                  pending={rename.isPending}
                  onCancel={() => setRenaming(null)}
                  onSubmit={(filename) => rename.mutate({ id: file.id, filename })}
                />
              ) : (
                <>
                  <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                    {file.filename}
                  </span>
                  <span className="hidden text-xs tabular-nums text-muted-foreground sm:inline">
                    {readableSize(file.size_bytes)}
                  </span>
                  <span className="hidden text-xs text-muted-foreground md:inline">
                    <When iso={file.created_at} />
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    {shownState(file.state).working ? (
                      <Loader2
                        aria-hidden="true"
                        className="size-3 animate-spin text-muted-foreground motion-reduce:animate-none"
                      />
                    ) : null}
                    <Pill tone={shownState(file.state).tone}>
                      {shownState(file.state).label}
                    </Pill>
                  </span>

                  <div className="ml-auto flex shrink-0 items-center gap-1">
                    {/* Decided server-side, so the button cannot appear where pressing
                        it would fail — `stored` and `rejected` are final answers. */}
                    {file.retryable ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        disabled={pending(file.id)}
                        onClick={() => retry.mutate(file.id)}
                      >
                        <RefreshCw aria-hidden="true" />
                        Try again
                      </Button>
                    ) : null}
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      disabled={pending(file.id) || file.state === "uploading"}
                      aria-label={`Download ${file.filename}`}
                      onClick={() => download.mutate(file.id)}
                    >
                      <Download aria-hidden="true" />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      disabled={pending(file.id)}
                      aria-label={`Rename ${file.filename}`}
                      onClick={() => {
                        setConfirming(null);
                        setRenaming(file.id);
                      }}
                    >
                      <Pencil aria-hidden="true" />
                    </Button>
                    {/* Two presses, not a `confirm()` dialog: removal deletes the stored
                        object as well as the row, and a native modal cannot be styled,
                        cannot be dismissed reliably on mobile, and blocks the event loop
                        while an upload is running. */}
                    {confirming === file.id ? (
                      <>
                        <Button
                          type="button"
                          variant="destructive"
                          size="sm"
                          disabled={pending(file.id)}
                          onClick={() => remove.mutate(file.id)}
                        >
                          Remove for good
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          aria-label="Keep this file"
                          onClick={() => setConfirming(null)}
                        >
                          <X aria-hidden="true" />
                        </Button>
                      </>
                    ) : (
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        disabled={pending(file.id)}
                        aria-label={`Remove ${file.filename}`}
                        className="text-destructive hover:text-destructive"
                        onClick={() => {
                          setRenaming(null);
                          setConfirming(file.id);
                        }}
                      >
                        <Trash2 aria-hidden="true" />
                      </Button>
                    )}
                  </div>
                </>
              )}

              {/* The server's own sentence about a file it refused, or kept without
                  indexing. Never composed here — the API is the only thing that knows
                  why, and inventing a reason is how an interface starts lying about
                  which formats work. */}
              {file.detail ? (
                <p className="w-full text-xs leading-relaxed text-muted-foreground">
                  {file.detail}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * Renaming in place.
 *
 * A form rather than a `prompt()`, so the value arrives pre-filled, Escape abandons the
 * change, and the submit is a real submit — which is what makes Enter work without a
 * keydown handler pretending to be one.
 */
function RenameForm({
  file,
  pending,
  onCancel,
  onSubmit,
}: {
  file: BasketFile;
  pending: boolean;
  onCancel: () => void;
  onSubmit: (filename: string) => void;
}) {
  const [value, setValue] = useState(file.filename);
  const unchanged = value.trim() === "" || value.trim() === file.filename;

  return (
    <form
      className="flex min-w-0 flex-1 items-center gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (!unchanged) onSubmit(value.trim());
      }}
    >
      <label htmlFor={`rename-${file.id}`} className="sr-only">
        New name for {file.filename}
      </label>
      <input
        id={`rename-${file.id}`}
        autoFocus
        value={value}
        maxLength={255}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") onCancel();
        }}
        className="h-9 min-w-0 flex-1 rounded-lg border border-hairline-strong bg-background px-3 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
      />
      <Button
        type="submit"
        variant="ghost"
        size="icon-sm"
        disabled={pending || unchanged}
        aria-label="Save the new name"
      >
        <Check aria-hidden="true" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        onClick={onCancel}
        aria-label="Cancel renaming"
      >
        <X aria-hidden="true" />
      </Button>
    </form>
  );
}
