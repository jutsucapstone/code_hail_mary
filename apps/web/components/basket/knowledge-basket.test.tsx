import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { KnowledgeBasket } from "@/components/basket/knowledge-basket";
import { envelope, type Json } from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The Knowledge Basket panel, against a scripted API and a scripted upload.
 *
 * Two properties are worth a frontend test at all, and both are ones a backend test
 * cannot see:
 *
 *   * **the bytes never reach the API.** The file goes to the signed URL and the API is
 *     told afterwards. A regression here would be invisible in production until somebody
 *     uploaded something over 32 MiB and Cloud Run rejected the request;
 *   * **the state shown is the state the server sent.** A `stored` file must not read as
 *     searchable, and a state this build does not recognise must not sit under a spinner
 *     for ever.
 *
 * Whether the API *itself* refuses a type, scopes a listing to one owner or signs a
 * bounded URL is proven by `test_basket.py` against real Postgres — never here, because a
 * scripted `fetch` agrees with whatever the component asks it.
 */

const FILE_ID = "77777777-7777-4777-8777-777777777777";

function aFile(overrides: Json = {}): Json {
  return {
    id: FILE_ID,
    owner_user_id: "44444444-4444-4444-8444-444444444444",
    filename: "handover notes.txt",
    content_type: "text/plain",
    size_bytes: 2048,
    state: "ready",
    detail: null,
    searchable: true,
    extracted_chars: 400,
    retryable: false,
    created_at: "2026-09-08T10:30:00Z",
    updated_at: "2026-09-08T10:31:00Z",
    ...overrides,
  };
}

interface Scripted {
  status: number;
  body: Json | null;
}

/**
 * A `fetch` that answers by looking at the request, not by position.
 *
 * The panel fires a listing on mount, a second after any mutation, and a third after the
 * search debounce — positional scripting hands the wrong body to whichever asked first
 * the moment a test adds an interaction.
 */
function fakeApi(handler: (url: string, init: RequestInit) => Scripted) {
  const fetchMock = vi.fn((input: unknown, init: RequestInit = {}) => {
    const { status, body } = handler(String(input), init);
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** A listing and nothing else. Anything unscripted comes back as a 404 envelope. */
function listing(...items: Json[]) {
  return fakeApi((url, init) => {
    if ((init.method ?? "GET") === "GET" && url.includes("/v1/basket/files")) {
      return { status: 200, body: { items } };
    }
    return { status: 404, body: envelope("not_found", "Not found.") };
  });
}

/**
 * `XMLHttpRequest`, scripted.
 *
 * The panel uses one rather than `fetch` because only `XMLHttpRequest` reports upload
 * progress, so a fake is the only way to reach the progress and cancel paths at all.
 */
class FakeXhr {
  static instances: FakeXhr[] = [];
  /** `"hold"` leaves the upload in flight, which is where cancel and progress live. */
  static behaviour: { status: number } | "hold" = { status: 200 };

  method = "";
  url = "";
  headers: Record<string, string> = {};
  sent: unknown = null;
  status = 0;
  upload = { onprogress: null as ((event: ProgressEvent) => void) | null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;

  constructor() {
    FakeXhr.instances.push(this);
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }

  send(body: unknown) {
    this.sent = body;
    if (FakeXhr.behaviour !== "hold") {
      this.progress(100);
      this.status = FakeXhr.behaviour.status;
      this.onload?.();
    }
  }

  abort() {
    this.onabort?.();
  }

  progress(percent: number) {
    this.upload.onprogress?.({
      lengthComputable: true,
      loaded: percent,
      total: 100,
    } as ProgressEvent);
  }
}

beforeEach(() => {
  FakeXhr.instances = [];
  FakeXhr.behaviour = { status: 200 };
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
});

/** Put a file on the hidden input, past the `accept` hint. */
async function choose(file: File) {
  await userEvent.upload(screen.getByLabelText(/Drop files here/), file, {
    applyAccept: false,
  });
}

describe("what the list says about a file", () => {
  it("calls a stored file stored, and never searchable", async () => {
    listing(
      aFile({
        filename: "walkthrough.mp4",
        content_type: "video/mp4",
        state: "stored",
        searchable: false,
        extracted_chars: null,
        detail: "Video is kept and downloadable, but its speech is not transcribed.",
      }),
    );
    renderWithQuery(<KnowledgeBasket />);

    expect(await screen.findByText("Stored")).toBeInTheDocument();
    expect(screen.queryByText("Searchable")).not.toBeInTheDocument();
    // The server's own sentence, rendered verbatim. Composing one here is how an
    // interface starts claiming a format works.
    expect(
      screen.getByText(/kept and downloadable, but its speech is not transcribed/),
    ).toBeInTheDocument();
  });

  it("offers a retry only where the server said one would do something", async () => {
    listing(
      aFile({ state: "failed", detail: "That PDF could not be read.", retryable: true }),
      aFile({
        id: "88888888-8888-4888-8888-888888888888",
        filename: "photo.png",
        state: "stored",
        retryable: false,
      }),
    );
    renderWithQuery(<KnowledgeBasket />);

    // One button for two rows: `retryable` is decided server-side precisely so the
    // control cannot appear where pressing it would fail.
    await screen.findByText("Could not read");
    expect(screen.getAllByRole("button", { name: "Try again" })).toHaveLength(1);
  });

  it("shows an unrecognised state as itself rather than as one it knows", async () => {
    // A state this build has not heard of means the API is ahead of the browser. Showing
    // the raw value says "something is there"; mapping it onto a known label would say
    // something specific and false.
    listing(aFile({ state: "transmogrifying" }));
    renderWithQuery(<KnowledgeBasket />);

    expect(await screen.findByText("transmogrifying")).toBeInTheDocument();
    expect(screen.queryByText("Indexing")).not.toBeInTheDocument();
  });

  it("polls while a file is still being read, and stops when it is done", async () => {
    // Extraction is durable work on a queue, so the row changes without the browser
    // doing anything — polling is how it notices.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    onTestFinished(() => {
      vi.useRealTimers();
    });
    let state = "extracting";
    const fetchMock = fakeApi(() => ({ status: 200, body: { items: [aFile({ state })] } }));
    renderWithQuery(<KnowledgeBasket />);
    await screen.findByText("Reading");

    const whileWorking = fetchMock.mock.calls.length;
    state = "ready";
    await act(() => vi.advanceTimersByTimeAsync(3500));
    await screen.findByText("Searchable");
    expect(fetchMock.mock.calls.length).toBeGreaterThan(whileWorking);

    // And now it must stop. A fixed interval would be a permanent timer on a page whose
    // files all finished days ago — and an unrecognised state, if it counted as working,
    // would poll for ever with nothing ever changing.
    const whenSettled = fetchMock.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchMock.mock.calls.length).toBe(whenSettled);
  });

  it("does not poll for a state it does not recognise", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    onTestFinished(() => {
      vi.useRealTimers();
    });
    const fetchMock = fakeApi(() => ({
      status: 200,
      body: { items: [aFile({ state: "transmogrifying" })] },
    }));
    renderWithQuery(<KnowledgeBasket />);
    await screen.findByText("transmogrifying");

    const settled = fetchMock.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchMock.mock.calls.length).toBe(settled);
  });

  it("says nothing is there rather than rendering an empty list", async () => {
    listing();
    renderWithQuery(<KnowledgeBasket />);

    expect(await screen.findByText("Nothing here yet")).toBeInTheDocument();
  });

  it("renders a refusal as a refusal, with the reference", async () => {
    fakeApi(() => ({
      status: 503,
      body: envelope("unavailable", "File storage is not configured.", "req-7"),
    }));
    renderWithQuery(<KnowledgeBasket />);

    expect(await screen.findByText("File storage is not configured.")).toBeInTheDocument();
    expect(screen.getByText(/req-7/)).toBeInTheDocument();
  });
});

describe("uploading a file", () => {
  it("sends the bytes to storage and never to the API", async () => {
    const fetchMock = fakeApi((url, init) => {
      const method = init.method ?? "GET";
      if (method === "GET") return { status: 200, body: { items: [] } };
      if (url.endsWith("/complete")) return { status: 200, body: aFile() };
      return {
        status: 201,
        body: {
          file_id: FILE_ID,
          url: "https://storage.googleapis.com/jutsu-basket/org/1/2?X-Goog-Signature=abc",
          headers: {
            "content-type": "text/plain",
            "x-goog-content-length-range": "5,5",
          },
          expires_in_seconds: 900,
        },
      };
    });
    renderWithQuery(<KnowledgeBasket />);
    await screen.findByText("Nothing here yet");

    await choose(new File(["hello"], "notes.txt", { type: "text/plain" }));

    await waitFor(() => expect(FakeXhr.instances).toHaveLength(1));
    const [upload] = FakeXhr.instances;
    expect(upload.method).toBe("PUT");
    expect(upload.url).toContain("storage.googleapis.com");
    // Verbatim: the headers were signed, so altering one makes Cloud Storage reject the
    // PUT rather than the API rejecting it here.
    expect(upload.headers).toEqual({
      "content-type": "text/plain",
      "x-goog-content-length-range": "5,5",
    });
    expect(upload.sent).toBeInstanceOf(File);

    // Nothing carrying the file's bytes went to the API — only the ticket request, which
    // sends a name, a type and a length.
    for (const [, init] of fetchMock.mock.calls) {
      const body = (init as RequestInit | undefined)?.body;
      expect(typeof body === "string" || body === undefined).toBe(true);
      if (typeof body === "string") expect(body).not.toContain("hello");
    }
  });

  it("declares the type from the extension, because the browser's guess is unreliable", async () => {
    // Windows reports `.csv` as `application/vnd.ms-excel`, and a signed URL pinned to
    // that refuses the very upload it was minted for.
    let declared: Json | null = null;
    fakeApi((url, init) => {
      const method = init.method ?? "GET";
      if (method === "GET") return { status: 200, body: { items: [] } };
      if (url.endsWith("/complete")) return { status: 200, body: aFile() };
      declared = JSON.parse(String(init.body)) as Json;
      return {
        status: 201,
        body: { file_id: FILE_ID, url: "https://storage/x", headers: {}, expires_in_seconds: 900 },
      };
    });
    renderWithQuery(<KnowledgeBasket />);
    await screen.findByText("Nothing here yet");

    await choose(
      new File(["a,b"], "rows.csv", { type: "application/vnd.ms-excel" }),
    );

    await waitFor(() => expect(declared).not.toBeNull());
    expect(declared).toEqual({
      filename: "rows.csv",
      content_type: "text/csv",
      size_bytes: 3,
    });
  });

  it("tells the API only once storage has the object", async () => {
    FakeXhr.behaviour = "hold";
    const fetchMock = fakeApi((url, init) => {
      const method = init.method ?? "GET";
      if (method === "GET") return { status: 200, body: { items: [] } };
      if (url.endsWith("/complete")) return { status: 200, body: aFile() };
      return {
        status: 201,
        body: { file_id: FILE_ID, url: "https://storage/x", headers: {}, expires_in_seconds: 900 },
      };
    });
    renderWithQuery(<KnowledgeBasket />);
    await screen.findByText("Nothing here yet");

    await choose(new File(["hello"], "notes.txt", { type: "text/plain" }));
    await waitFor(() => expect(FakeXhr.instances).toHaveLength(1));

    // The upload is still in flight, so completing now would record a state for an
    // object that is not there.
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).endsWith("/complete")),
    ).toBe(false);

    FakeXhr.instances[0].status = 200;
    FakeXhr.instances[0].onload?.();

    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/complete"))).toBe(
        true,
      ),
    );
  });

  it("reports real progress rather than an animation", async () => {
    FakeXhr.behaviour = "hold";
    fakeApi((url, init) => {
      const method = init.method ?? "GET";
      if (method === "GET") return { status: 200, body: { items: [] } };
      return {
        status: 201,
        body: { file_id: FILE_ID, url: "https://storage/x", headers: {}, expires_in_seconds: 900 },
      };
    });
    renderWithQuery(<KnowledgeBasket />);
    await screen.findByText("Nothing here yet");

    await choose(new File(["hello"], "notes.txt", { type: "text/plain" }));
    await waitFor(() => expect(FakeXhr.instances).toHaveLength(1));

    expect(await screen.findByText("0%")).toBeInTheDocument();
    // The provider fires this from outside React, exactly as a real upload does.
    act(() => FakeXhr.instances[0].progress(42));
    expect(await screen.findByText("42%")).toBeInTheDocument();
  });

  it("cancels an upload that is still in flight", async () => {
    FakeXhr.behaviour = "hold";
    const fetchMock = fakeApi((url, init) => {
      const method = init.method ?? "GET";
      if (method === "GET") return { status: 200, body: { items: [] } };
      return {
        status: 201,
        body: { file_id: FILE_ID, url: "https://storage/x", headers: {}, expires_in_seconds: 900 },
      };
    });
    renderWithQuery(<KnowledgeBasket />);
    await screen.findByText("Nothing here yet");

    await choose(new File(["hello"], "notes.txt", { type: "text/plain" }));
    await userEvent.click(
      await screen.findByRole("button", { name: "Cancel uploading notes.txt" }),
    );

    await waitFor(() =>
      expect(screen.queryByLabelText("Uploads in progress")).not.toBeInTheDocument(),
    );
    // A cancelled upload must not be completed: doing so would claim a state for a
    // half-written object.
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).endsWith("/complete")),
    ).toBe(false);
  });

  it("refuses an oversize file before spending a request on it", async () => {
    const fetchMock = listing();
    renderWithQuery(<KnowledgeBasket />);
    await screen.findByText("Nothing here yet");
    const before = fetchMock.mock.calls.length;

    const huge = new File(["x"], "recording.mp4", { type: "video/mp4" });
    Object.defineProperty(huge, "size", { value: 600 * 1024 * 1024 });
    await choose(huge);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That file is larger than 512 MB.",
    );
    expect(fetchMock.mock.calls).toHaveLength(before);
    expect(FakeXhr.instances).toHaveLength(0);
  });

  it("says a storage refusal in words, without naming the bucket", async () => {
    FakeXhr.behaviour = { status: 403 };
    fakeApi((url, init) => {
      const method = init.method ?? "GET";
      if (method === "GET") return { status: 200, body: { items: [] } };
      return {
        status: 201,
        body: {
          file_id: FILE_ID,
          url: "https://storage.googleapis.com/jutsu-506513-basket/org/1/2",
          headers: {},
          expires_in_seconds: 900,
        },
      };
    });
    renderWithQuery(<KnowledgeBasket />);
    await screen.findByText("Nothing here yet");

    await choose(new File(["hello"], "notes.txt", { type: "text/plain" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Storage refused that upload (403).");
    expect(alert.textContent).not.toContain("jutsu-506513-basket");
  });
});

describe("the actions on a row", () => {
  it("sends the new name, and nothing else", async () => {
    let renamed: Json | null = null;
    fakeApi((url, init) => {
      const method = init.method ?? "GET";
      if (method === "GET") return { status: 200, body: { items: [aFile()] } };
      if (method === "PATCH") {
        renamed = JSON.parse(String(init.body)) as Json;
        return { status: 200, body: aFile({ filename: "March migration.txt" }) };
      }
      return { status: 404, body: envelope("not_found", "Not found.") };
    });
    renderWithQuery(<KnowledgeBasket />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Rename handover notes.txt" }),
    );
    const field = screen.getByLabelText(/New name for handover notes.txt/);
    await userEvent.clear(field);
    await userEvent.type(field, "March migration.txt");
    await userEvent.click(screen.getByRole("button", { name: "Save the new name" }));

    await waitFor(() => expect(renamed).toEqual({ filename: "March migration.txt" }));
  });

  it("abandons a rename on Escape without sending anything", async () => {
    const fetchMock = listing(aFile());
    renderWithQuery(<KnowledgeBasket />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Rename handover notes.txt" }),
    );
    await userEvent.type(screen.getByLabelText(/New name for/), "oops{Escape}");

    expect(screen.queryByLabelText(/New name for/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.every(([, init]) => (init as RequestInit).method === "GET")).toBe(
      true,
    );
  });

  it("takes two presses to remove a file", async () => {
    let deleted = false;
    fakeApi((url, init) => {
      const method = init.method ?? "GET";
      if (method === "DELETE") {
        deleted = true;
        return { status: 204, body: null };
      }
      return { status: 200, body: { items: deleted ? [] : [aFile()] } };
    });
    renderWithQuery(<KnowledgeBasket />);

    // The first press only arms it. Removal deletes the stored object as well as the
    // row, and there is no undo.
    await userEvent.click(
      await screen.findByRole("button", { name: "Remove handover notes.txt" }),
    );
    expect(deleted).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Remove for good" }));
    await waitFor(() => expect(deleted).toBe(true));
  });

  it("lets an armed removal be called off", async () => {
    let deleted = false;
    fakeApi((url, init) => {
      if ((init.method ?? "GET") === "DELETE") {
        deleted = true;
        return { status: 204, body: null };
      }
      return { status: 200, body: { items: [aFile()] } };
    });
    renderWithQuery(<KnowledgeBasket />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Remove handover notes.txt" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Keep this file" }));

    expect(screen.queryByRole("button", { name: "Remove for good" })).not.toBeInTheDocument();
    expect(deleted).toBe(false);
  });

  it("fetches a fresh signed URL rather than holding one", async () => {
    // The URL is short-lived and is minted per request after an authorization check, so
    // it is asked for at the moment of the press — never carried in the listing, where it
    // would sit in the query cache long after it stopped being valid.
    // jsdom's own `location` is unforgeable and would log "navigation not implemented",
    // so it is replaced for the duration of this test and put back afterwards — leaving
    // a stand-in in place would break any later test that reads the page's own URL.
    const assign = vi.fn();
    const original = Object.getOwnPropertyDescriptor(window, "location")!;
    onTestFinished(() => {
      Object.defineProperty(window, "location", original);
    });
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { href: original.value?.href ?? "http://localhost/", assign },
    });
    const fetchMock = fakeApi((url) =>
      url.endsWith("/download")
        ? { status: 200, body: { url: "https://storage/signed?X-Goog-Signature=abc" } }
        : { status: 200, body: { items: [aFile()] } },
    );
    renderWithQuery(<KnowledgeBasket />);

    // Nothing signed arrived with the listing.
    await screen.findByText("handover notes.txt");
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).endsWith("/download")),
    ).toBe(false);

    await userEvent.click(
      screen.getByRole("button", { name: "Download handover notes.txt" }),
    );

    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("https://storage/signed?X-Goog-Signature=abc"),
    );
  });

  it("asks the server to try a failed file again", async () => {
    let retried = false;
    fakeApi((url) => {
      if (url.endsWith("/retry")) {
        retried = true;
        return { status: 200, body: aFile({ state: "uploaded" }) };
      }
      return { status: 200, body: { items: [aFile({ state: "failed", retryable: true })] } };
    });
    renderWithQuery(<KnowledgeBasket />);

    await userEvent.click(await screen.findByRole("button", { name: "Try again" }));

    await waitFor(() => expect(retried).toBe(true));
  });
});

describe("finding a file", () => {
  it("searches server-side, once, after the typing settles", async () => {
    const fetchMock = fakeApi(() => ({ status: 200, body: { items: [aFile()] } }));
    renderWithQuery(<KnowledgeBasket />);
    await screen.findByText("handover notes.txt");

    await userEvent.type(screen.getByLabelText("Search your files"), "migration");

    // One request for nine keystrokes: the term is debounced, and the filter is the
    // server's — a client-side filter over one page would hide matches on the next.
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes("q=migration")),
      ).toBe(true),
    );
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).includes("q=")),
    ).toHaveLength(1);
  });
});
