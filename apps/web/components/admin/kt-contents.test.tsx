import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { KtContents } from "@/components/admin/kt-contents";
import { envelope, type Json } from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The curator's review of what a package shares (ADR 0027).
 *
 * Whether keeping a document back really removes it from Ask KT, citations and the report
 * is proven against real Postgres in `apps/api/tests/test_kt_package_boundary.py`; a
 * scripted `fetch` would agree with anything. What is proven here is the panel's honesty:
 * it shows titles and states, it sends exactly the document it names, it never offers to
 * share a document again on a closed package, and it says so when there is nothing to show.
 */

const toasts = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
}));

vi.mock("sonner", () => ({ toast: toasts }));

const PACKAGE_ID = "12121212-1212-4121-8121-121212121212";
const PRIVATE_ID = "dddddddd-dddd-4ddd-8ddd-ddddddddddd1";

beforeEach(() => {
  toasts.success.mockClear();
  toasts.warning.mockClear();
  toasts.error.mockClear();
});

function item(overrides: Json = {}): Json {
  return {
    document_id: "dddddddd-dddd-4ddd-8ddd-ddddddddddd0",
    title: "Orion architecture overview",
    source_system: "m365",
    created_at: "2026-09-01T10:00:00Z",
    attached_file: false,
    excluded: false,
    ...overrides,
  };
}

interface Scripted {
  status: number;
  body: Json | null;
}

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

/** The review's one GET, answered from a list; every write answered by `writes`. */
function review(
  items: Json[],
  writes: (url: string, init: RequestInit) => Scripted = () => ({
    status: 201,
    body: item({ excluded: true }),
  }),
) {
  return fakeApi((url, init) => {
    if ((init.method ?? "GET") === "GET" && url.includes("/contents")) {
      return { status: 200, body: { items, next_cursor: null } };
    }
    return writes(url, init);
  });
}

describe("what the review shows", () => {
  it("lists every document with its source, and marks what is kept back", async () => {
    review([
      item(),
      item({
        document_id: PRIVATE_ID,
        title: "Personal leave request",
        source_system: "gmail",
        excluded: true,
      }),
      item({
        document_id: "dddddddd-dddd-4ddd-8ddd-ddddddddddd2",
        title: "handover notes.txt",
        source_system: "basket",
        attached_file: true,
      }),
    ]);
    renderWithQuery(<KtContents packageId={PACKAGE_ID} closed={false} />);

    const list = await screen.findByRole("list", { name: "Shared documents" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("Personal leave request");
    expect(rows[1]).toHaveTextContent("Kept back");
    expect(rows[0]).not.toHaveTextContent("Kept back");
    expect(rows[2]).toHaveTextContent("Knowledge Basket");
    expect(screen.getByText("3 documents · 1 kept back")).toBeInTheDocument();
  });

  it("reads the package's contents route and nothing else", async () => {
    const fetchMock = review([item()]);
    renderWithQuery(<KtContents packageId={PACKAGE_ID} closed={false} />);
    await screen.findByText("Orion architecture overview");

    expect(new Set(fetchMock.mock.calls.map(([url]) => String(url)))).toEqual(
      new Set([`/api/jutsu/v1/kt/${PACKAGE_ID}/contents`]),
    );
  });

  it("says so when there is nothing to review, rather than rendering a blank", async () => {
    review([]);
    renderWithQuery(<KtContents packageId={PACKAGE_ID} closed={false} />);

    expect(await screen.findByText(/Nothing yet/)).toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });

  it("renders a refusal as a refusal", async () => {
    fakeApi(() => ({
      status: 500,
      body: envelope("internal", "Something broke.", "req-7"),
    }));
    renderWithQuery(<KtContents packageId={PACKAGE_ID} closed={false} />);

    expect(await screen.findByText("Something broke.")).toBeInTheDocument();
    expect(screen.getByText(/req-7/)).toBeInTheDocument();
  });
});

describe("keeping a document back", () => {
  it("posts exactly the document it names", async () => {
    const fetchMock = review([
      item(),
      item({ document_id: PRIVATE_ID, title: "Personal leave request" }),
    ]);
    renderWithQuery(<KtContents packageId={PACKAGE_ID} closed={false} />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Keep Personal leave request back" }),
    );

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([, init]) => (init as RequestInit)?.method === "POST",
      );
      expect(post).toBeDefined();
      expect(String(post![0])).toBe(`/api/jutsu/v1/kt/${PACKAGE_ID}/exclusions`);
      expect(JSON.parse(String((post![1] as RequestInit).body))).toEqual({
        document_id: PRIVATE_ID,
      });
    });
    await waitFor(() => expect(toasts.success).toHaveBeenCalled());
  });

  it("shares a kept-back document again by deleting its exclusion", async () => {
    const fetchMock = review(
      [item({ document_id: PRIVATE_ID, title: "Personal leave request", excluded: true })],
      () => ({ status: 204, body: null }),
    );
    renderWithQuery(<KtContents packageId={PACKAGE_ID} closed={false} />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Share Personal leave request again" }),
    );

    await waitFor(() => {
      const removal = fetchMock.mock.calls.find(
        ([, init]) => (init as RequestInit)?.method === "DELETE",
      );
      expect(removal).toBeDefined();
      expect(String(removal![0])).toBe(
        `/api/jutsu/v1/kt/${PACKAGE_ID}/exclusions/${PRIVATE_ID}`,
      );
    });
  });

  it("reports a refused write in the server's words", async () => {
    review([item()], () => ({
      status: 404,
      body: envelope("not_found", "That document is not part of this package."),
    }));
    renderWithQuery(<KtContents packageId={PACKAGE_ID} closed={false} />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Keep Orion architecture overview back" }),
    );

    await waitFor(() =>
      expect(toasts.error).toHaveBeenCalledWith("That document is not part of this package."),
    );
    expect(toasts.success).not.toHaveBeenCalled();
  });
});

describe("a closed package", () => {
  it("still keeps documents back but never offers to share one again", async () => {
    review([
      item(),
      item({ document_id: PRIVATE_ID, title: "Personal leave request", excluded: true }),
    ]);
    renderWithQuery(<KtContents packageId={PACKAGE_ID} closed />);

    expect(
      await screen.findByRole("button", { name: "Keep Orion architecture overview back" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Share Personal leave request again" }),
    ).toBeDisabled();
    expect(screen.getByText(/nothing can be shared again/)).toBeInTheDocument();
  });
});
