import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EvidenceSearch } from "@/components/product/evidence-search";

/**
 * The retrieval surface, against a scripted `fetch`.
 *
 * `lib/api.ts` is one `fetch` call wide, so stubbing it is the whole seam — and it is
 * what lets these tests assert the request *body*, which is how "the browser cannot
 * widen what it sees" is checked rather than asserted in a comment.
 *
 * No network, no API process, no provider. A frontend test that reached Vertex would
 * bill CI per assertion.
 */

type Json = Record<string, unknown>;

const CHUNK = "11111111-1111-4111-8111-111111111111";
const CHUNK_2 = "22222222-2222-4222-8222-222222222222";

function result(overrides: Json = {}): Json {
  return {
    chunk_id: CHUNK,
    document_id: "33333333-3333-4333-8333-333333333333",
    document_title: "California Update 5/17/01",
    source_system: "local",
    text: "the masked passage",
    char_start: 986,
    char_end: 3906,
    score: 0.6655,
    occurred_at: "2001-05-17T00:00:00Z",
    ...overrides,
  };
}

function page(overrides: Json = {}): Json {
  return {
    items: [result()],
    stats: { attempts: 1, ef_search: 100, returned: 1, elapsed_ms: 77, exhausted: false },
    next_cursor: null,
    query_tokens: 12,
    ...overrides,
  };
}

function envelope(code: string, message: string): Json {
  return { error: { code, message, details: {} }, request_id: "req-abc" };
}

/** Script `fetch`. Returns the mock so a test can read what the client sent. */
function scriptFetch(...responses: Array<{ status: number; body: Json }>) {
  const fetchMock = vi.fn();
  for (const { status, body } of responses) {
    fetchMock.mockResolvedValueOnce({
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    });
  }
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The JSON body of the nth `fetch` call. */
function sentBody(fetchMock: ReturnType<typeof vi.fn>, index = 0): Json {
  const init = fetchMock.mock.calls[index][1] as RequestInit;
  return JSON.parse(String(init.body)) as Json;
}

async function search(user: ReturnType<typeof userEvent.setup>, query = "government affairs") {
  await user.type(screen.getByLabelText("Search query"), query);
  await user.click(screen.getByRole("button", { name: /^search$/i }));
}

beforeEach(() => {
  // The client reads a CSRF cookie; absent is fine, but jsdom must not carry one over.
  document.cookie = "";
});

describe("a successful search", () => {
  it("renders the returned passages", async () => {
    const user = userEvent.setup();
    scriptFetch({ status: 200, body: page() });
    render(<EvidenceSearch />);

    await search(user);

    expect(await screen.findByText("California Update 5/17/01")).toBeInTheDocument();
    expect(screen.getByText("the masked passage")).toBeInTheDocument();
    expect(screen.getByTestId("search-stats")).toHaveTextContent("1 result in 77 ms");
  });

  it("posts to the search endpoint", async () => {
    const user = userEvent.setup();
    const fetchMock = scriptFetch({ status: 200, body: page() });
    render(<EvidenceSearch />);

    await search(user);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/jutsu/v1/search");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
  });
});

describe("the request the browser sends", () => {
  it("carries only query, k and cursor", async () => {
    const user = userEvent.setup();
    const fetchMock = scriptFetch({ status: 200, body: page() });
    render(<EvidenceSearch />);

    await search(user);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(Object.keys(sentBody(fetchMock)).sort()).toEqual(["cursor", "k", "query"]);
  });

  it.each([
    "org_id",
    "user_id",
    "principals",
    "acl",
    "min_score",
    "filters",
    "tenant",
  ])("never sends %s", async (field) => {
    const user = userEvent.setup();
    const fetchMock = scriptFetch({ status: 200, body: page() });
    render(<EvidenceSearch />);

    await search(user);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(sentBody(fetchMock)).not.toHaveProperty(field);
  });
});

describe("empty results", () => {
  it("explains that access filters the results, and is not an error", async () => {
    const user = userEvent.setup();
    scriptFetch({
      status: 200,
      body: page({
        items: [],
        stats: { attempts: 2, ef_search: 400, returned: 0, elapsed_ms: 16, exhausted: true },
      }),
    });
    render(<EvidenceSearch />);

    await search(user);

    expect(await screen.findByText(/no matching evidence/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("exhausted", () => {
  it("is informational, never an error", async () => {
    const user = userEvent.setup();
    scriptFetch({
      status: 200,
      body: page({
        stats: { attempts: 2, ef_search: 400, returned: 1, elapsed_ms: 20, exhausted: true },
      }),
    });
    render(<EvidenceSearch />);

    await search(user);

    expect(await screen.findByTestId("search-exhausted")).toBeInTheDocument();
    // The distinction that matters: `role="alert"` is what a screen reader announces as
    // a problem, and "you may not see more" is not one.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("is absent when the search was not exhausted", async () => {
    const user = userEvent.setup();
    scriptFetch({ status: 200, body: page() });
    render(<EvidenceSearch />);

    await search(user);

    await screen.findByText("California Update 5/17/01");
    expect(screen.queryByTestId("search-exhausted")).not.toBeInTheDocument();
  });
});

describe("pagination", () => {
  it("sends the cursor back and appends the next page", async () => {
    const user = userEvent.setup();
    const fetchMock = scriptFetch(
      { status: 200, body: page({ next_cursor: "CURSOR-1" }) },
      {
        status: 200,
        body: page({
          items: [result({ chunk_id: CHUNK_2, document_title: "CA Price Issues" })],
          next_cursor: null,
        }),
      },
    );
    render(<EvidenceSearch />);

    await search(user);
    await user.click(await screen.findByRole("button", { name: /load more/i }));

    expect(await screen.findByText("CA Price Issues")).toBeInTheDocument();
    // The first page is still on screen: a keyset cursor appends, it does not replace.
    expect(screen.getByText("California Update 5/17/01")).toBeInTheDocument();
    expect(sentBody(fetchMock, 1).cursor).toBe("CURSOR-1");
    expect(sentBody(fetchMock, 1).query).toBe("government affairs");
  });

  it("offers no control when there is no next page", async () => {
    const user = userEvent.setup();
    scriptFetch({ status: 200, body: page({ next_cursor: null }) });
    render(<EvidenceSearch />);

    await search(user);

    await screen.findByText("California Update 5/17/01");
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
  });
});

describe("documented failures", () => {
  it("401 asks the reader to sign in again, and offers no retry", async () => {
    const user = userEvent.setup();
    scriptFetch({ status: 401, body: envelope("unauthenticated", "Not signed in.") });
    render(<EvidenceSearch />);

    await search(user);

    expect(await screen.findByRole("alert")).toHaveTextContent(/session has expired/i);
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
  });

  it("422 shows the validation message without a retry", async () => {
    const user = userEvent.setup();
    scriptFetch({
      status: 422,
      body: envelope("validation_failed", "cursor is not a valid pagination token"),
    });
    render(<EvidenceSearch />);

    await search(user);

    expect(await screen.findByRole("alert")).toHaveTextContent(/not a valid pagination token/i);
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
  });

  it("429 surfaces the limit and does offer a retry", async () => {
    const user = userEvent.setup();
    scriptFetch({
      status: 429,
      body: envelope("rate_limited", "Too many searches. Try again shortly."),
    });
    render(<EvidenceSearch />);

    await search(user);

    expect(await screen.findByRole("alert")).toHaveTextContent(/too many searches/i);
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("503 reports the provider as unavailable and offers a retry", async () => {
    const user = userEvent.setup();
    scriptFetch({
      status: 503,
      body: envelope("service_unavailable", "The embedding provider is unavailable."),
    });
    render(<EvidenceSearch />);

    await search(user);

    expect(await screen.findByRole("alert")).toHaveTextContent(/provider is unavailable/i);
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("shows the request id, which identifies a request and not a person", async () => {
    const user = userEvent.setup();
    scriptFetch({ status: 503, body: envelope("service_unavailable", "Unavailable.") });
    render(<EvidenceSearch />);

    await search(user);

    expect(await screen.findByText(/req-abc/i)).toBeInTheDocument();
  });

  it("renders a body that is not an envelope without crashing", async () => {
    const user = userEvent.setup();
    scriptFetch({ status: 502, body: { nonsense: true } });
    render(<EvidenceSearch />);

    await search(user);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});

describe("loading", () => {
  it("announces itself while the search is in flight", async () => {
    const user = userEvent.setup();
    let release: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(
        pending.then(() => ({ ok: true, status: 200, json: async () => page() })),
      ),
    );
    render(<EvidenceSearch />);

    await search(user);

    expect(await screen.findByText("Searching the corpus")).toBeInTheDocument();
    release(null);
    expect(await screen.findByText("California Update 5/17/01")).toBeInTheDocument();
  });
});

describe("evidence", () => {
  it("fetches the source span from the evidence endpoint, not from the result", async () => {
    const user = userEvent.setup();
    const fetchMock = scriptFetch(
      { status: 200, body: page() },
      {
        status: 200,
        body: {
          chunk_id: CHUNK,
          document_id: "33333333-3333-4333-8333-333333333333",
          document_title: "California Update 5/17/01",
          source_system: "local",
          text: "the span from the evidence endpoint",
          char_start: 986,
          char_end: 3906,
          occurred_at: "2001-05-17T00:00:00Z",
        },
      },
    );
    render(<EvidenceSearch />);

    await search(user);
    await user.click(await screen.findByRole("button", { name: /view source span/i }));

    expect(await screen.findByText("the span from the evidence endpoint")).toBeInTheDocument();
    expect(fetchMock.mock.calls[1][0]).toBe(`/api/jutsu/v1/evidence/${CHUNK}`);
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe("GET");
  });

  it("reports a failed evidence fetch without destroying the results", async () => {
    const user = userEvent.setup();
    scriptFetch(
      { status: 200, body: page() },
      { status: 404, body: envelope("not_found", "No such evidence.") },
    );
    render(<EvidenceSearch />);

    await search(user);
    await user.click(await screen.findByRole("button", { name: /view source span/i }));

    expect(await screen.findByText("No such evidence.")).toBeInTheDocument();
    expect(screen.getByText("California Update 5/17/01")).toBeInTheDocument();
  });

  it("never slices the masked text with the original offsets", async () => {
    const user = userEvent.setup();
    scriptFetch({ status: 200, body: page() });
    render(<EvidenceSearch />);

    await search(user);

    // The whole masked passage is rendered. If the component sliced it with
    // char_start/char_end — offsets into the ORIGINAL body — this text would be absent
    // or truncated, which is the trap CLAUDE.md records.
    expect(await screen.findByText("the masked passage")).toBeInTheDocument();
  });
});
