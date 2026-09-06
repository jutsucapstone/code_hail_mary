import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "sonner";

import { KtInsightsList } from "@/components/kt/kt-insights";
import { KtShell } from "@/components/kt/kt-shell";
import {
  callIndexFor,
  calledMethod,
  calledUrl,
  envelope,
  scriptFetch,
  sentBody,
  type Json,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The knowledge tabs' claim cards, against a scripted API.
 *
 * Pinned here: the card's actions send exactly the documented requests — save a claim,
 * fetch its masked source span, mark it done or unclear, clear the mark — and a claim the
 * recipient has already marked renders that mark. The list makes several requests on
 * mount (the claim, the insights, the progress marks), so every assertion finds its
 * request by URL rather than by position.
 */

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/kt/KT-JUTSU-AAAA0001/decisions",
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

beforeEach(() => {
  vi.clearAllMocks();
});

const CODE = "KT-JUTSU-AAAA0001";
const BASE = `/api/jutsu/v1/kt/${CODE}`;
const CLAIM_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const CHUNK_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

function recipientPackage(overrides: Json = {}): Json {
  return {
    kt_code: CODE,
    status: "claimed",
    scope: ["documents", "profile", "decisions"],
    period_start: null,
    period_end: null,
    expires_at: "2026-10-01T00:00:00Z",
    created_at: "2026-09-01T00:00:00Z",
    subject: {
      display_name: "Grace Hopper",
      designation: "Staff Engineer",
      department: "Platform",
    },
    ...overrides,
  };
}

function insight(overrides: Json = {}): Json {
  return {
    id: CLAIM_ID,
    claim_type: "decision",
    name: "Ledger storage",
    summary: "Move the ledger to Postgres",
    quote: "we will move the ledger to Postgres by Q3",
    confidence: 0.91,
    date: "2026-06-12",
    occurred_at: "2026-06-12T09:00:00Z",
    chunk_id: CHUNK_ID,
    document_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    document_title: "Platform sync, 12 June",
    source_system: "gmail",
    ...overrides,
  };
}

const HEADLINE = "Ledger storage — Move the ledger to Postgres";

function mount() {
  return renderWithQuery(
    <KtShell code={CODE}>
      <KtInsightsList claimType="decision" title="Decisions" emptyWord="decisions" />
    </KtShell>,
  );
}

/** Call 0 is the shell's claim; then the insights page; then the progress marks. */
function mountWith(progressItems: Json[], ...more: { status: number; body: Json | null }[]) {
  const fetchMock = scriptFetch(
    { status: 200, body: recipientPackage() },
    { status: 200, body: { items: [insight()] } },
    { status: 200, body: { items: progressItems } },
    ...more,
  );
  mount();
  return fetchMock;
}

describe("the KT claim cards", () => {
  it("fetches the progress marks once alongside the insights", async () => {
    const fetchMock = mountWith([]);

    expect(await screen.findByText(HEADLINE)).toBeInTheDocument();
    expect(calledUrl(fetchMock, callIndexFor(fetchMock, "/progress"))).toBe(`${BASE}/progress`);
    expect(fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/progress"))).toHaveLength(1);
    // Unmarked: no pill, and nothing to clear.
    expect(screen.queryByRole("button", { name: `Clear progress mark: ${HEADLINE}` })).not.toBeInTheDocument();
  });

  it("saves a claim with POST {kind: claim, ref_id}", async () => {
    const fetchMock = mountWith([], {
      status: 201,
      body: {
        id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        kind: "claim",
        ref_id: CLAIM_ID,
        label: HEADLINE,
        note: null,
        tab: "decisions",
        available: true,
        created_at: "2026-09-02T10:00:00Z",
        updated_at: "2026-09-02T10:00:00Z",
      },
    });

    await userEvent.click(
      await screen.findByRole("button", { name: `Save to your items: ${HEADLINE}` }),
    );

    const post = callIndexFor(fetchMock, "/bookmarks");
    expect(calledUrl(fetchMock, post)).toBe(`${BASE}/bookmarks`);
    expect(calledMethod(fetchMock, post)).toBe("POST");
    expect(sentBody(fetchMock, post)).toEqual({ kind: "claim", ref_id: CLAIM_ID });
    await waitFor(() =>
      expect(vi.mocked(toast.success)).toHaveBeenCalledWith("Saved to your items."),
    );
  });

  it("fetches the source span by chunk id and shows the masked text as returned", async () => {
    const masked = "we will move the ledger to Postgres by Q3, said [EMAIL_A7] on the call";
    const fetchMock = mountWith([], {
      status: 200,
      body: {
        chunk_id: CHUNK_ID,
        document_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        document_title: "Platform sync, 12 June",
        source_system: "gmail",
        occurred_at: "2026-06-12T09:00:00Z",
        text: masked,
        char_start: 120,
        char_end: 190,
      },
    });

    await userEvent.click(
      await screen.findByRole("button", { name: `View source for: ${HEADLINE}` }),
    );

    const get = callIndexFor(fetchMock, "/v1/evidence/");
    expect(calledUrl(fetchMock, get)).toBe(`/api/jutsu/v1/evidence/${CHUNK_ID}`);
    expect(calledMethod(fetchMock, get)).toBe("GET");
    // The whole masked string, pseudonym included — never a char_start/char_end slice.
    expect(await screen.findByText(masked)).toBeInTheDocument();
    expect(screen.getByText(/chars 120–190/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: `View source for: ${HEADLINE}` })).toBeDisabled();
  });

  it("reports a source span the server refuses, in place", async () => {
    mountWith([], {
      status: 404,
      body: envelope("not_found", "No evidence matches that reference."),
    });

    await userEvent.click(
      await screen.findByRole("button", { name: `View source for: ${HEADLINE}` }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("No evidence matches that reference.");
  });

  it("marks a claim done with PUT /progress/claim:{id} {state: done}", async () => {
    const fetchMock = mountWith(
      [],
      {
        status: 200,
        body: { item_key: `claim:${CLAIM_ID}`, state: "done", updated_at: "2026-09-03T00:00:00Z" },
      },
      // The invalidated progress list, refetched.
      {
        status: 200,
        body: {
          items: [{ item_key: `claim:${CLAIM_ID}`, state: "done", updated_at: "2026-09-03T00:00:00Z" }],
        },
      },
    );

    await userEvent.click(await screen.findByRole("button", { name: `Mark done: ${HEADLINE}` }));

    const put = callIndexFor(fetchMock, `/progress/claim%3A${CLAIM_ID}`);
    expect(calledUrl(fetchMock, put)).toBe(`${BASE}/progress/claim%3A${CLAIM_ID}`);
    expect(calledMethod(fetchMock, put)).toBe("PUT");
    expect(sentBody(fetchMock, put)).toEqual({ state: "done" });

    // The refetched mark lands on the card.
    const card = (await screen.findByText(HEADLINE)).closest("li");
    expect(card).not.toBeNull();
    expect(await within(card!).findByText("done")).toBeInTheDocument();
  });

  it("renders the pill for a claim already marked, and clears it with DELETE", async () => {
    const fetchMock = mountWith(
      [{ item_key: `claim:${CLAIM_ID}`, state: "unclear", updated_at: "2026-09-03T00:00:00Z" }],
      { status: 204, body: null },
      { status: 200, body: { items: [] } },
    );

    const card = (await screen.findByText(HEADLINE)).closest("li");
    expect(card).not.toBeNull();
    expect(within(card!).getByText("unclear")).toBeInTheDocument();
    // Already unclear: the button that would say so again is not offered.
    expect(
      within(card!).queryByRole("button", { name: `Still unclear: ${HEADLINE}` }),
    ).not.toBeInTheDocument();
    expect(within(card!).getByRole("button", { name: `Mark done: ${HEADLINE}` })).toBeInTheDocument();

    await userEvent.click(within(card!).getByRole("button", { name: `Clear progress mark: ${HEADLINE}` }));

    const del = callIndexFor(fetchMock, `/progress/claim%3A${CLAIM_ID}`);
    expect(calledMethod(fetchMock, del)).toBe("DELETE");
    await waitFor(() => expect(within(card!).queryByText("unclear")).not.toBeInTheDocument());
  });

  it("marks a claim still unclear with {state: unclear}", async () => {
    const fetchMock = mountWith(
      [],
      {
        status: 200,
        body: { item_key: `claim:${CLAIM_ID}`, state: "unclear", updated_at: "2026-09-03T00:00:00Z" },
      },
      {
        status: 200,
        body: {
          items: [{ item_key: `claim:${CLAIM_ID}`, state: "unclear", updated_at: "2026-09-03T00:00:00Z" }],
        },
      },
    );

    await userEvent.click(await screen.findByRole("button", { name: `Still unclear: ${HEADLINE}` }));

    const put = callIndexFor(fetchMock, `/progress/claim%3A${CLAIM_ID}`);
    expect(sentBody(fetchMock, put)).toEqual({ state: "unclear" });
    expect(await screen.findByText("unclear")).toBeInTheDocument();
  });

  it("keeps the claims readable when the progress marks fail, and says so", async () => {
    scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [insight()] } },
      { status: 503, body: envelope("unavailable", "The database is not reachable.") },
    );
    mount();

    expect(await screen.findByText(HEADLINE)).toBeInTheDocument();
    expect(await screen.findByText(/your progress marks did not load/i)).toHaveTextContent(
      "The database is not reachable.",
    );
  });

  it("surfaces a refused progress write as a toast", async () => {
    mountWith([], {
      status: 429,
      body: envelope("rate_limited", "Too many requests. Try again in a minute."),
    });

    await userEvent.click(await screen.findByRole("button", { name: `Mark done: ${HEADLINE}` }));

    await waitFor(() =>
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith("Too many requests. Try again in a minute."),
    );
  });
});
