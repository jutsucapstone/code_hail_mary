import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { KtAttachments } from "@/components/admin/kt-attachments";
import { envelope, type Json } from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The curator's side of package-scoped basket sharing (ADR 0021).
 *
 * What is worth a frontend test here is the honesty of the panel, not the authorization —
 * that is proven against real Postgres in `apps/api/tests/test_kt_files.py`, and a
 * scripted `fetch` would agree with whatever this component asked it.
 *
 * Two properties carry the file:
 *
 *   * the panel reports what the SERVER said landed, not what was asked for. Attaching
 *     five files and having three land is a normal outcome — a file already attached, or
 *     one belonging to somebody else — and saying "5 attached" is the kind of lie that is
 *     found weeks later;
 *   * a closed package offers no picker at all rather than a button that would 409.
 */

// `vi.hoisted`, because `vi.mock` is lifted to the top of the file and a plain const
// declared here would not exist yet when the factory runs.
const toasts = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
}));

// Sonner renders into a portal the panel does not own, so what the reader is told is
// only assertable at the call. This is the one place the count actually reaches a person.
vi.mock("sonner", () => ({ toast: toasts }));

const PACKAGE_ID = "12121212-1212-4121-8121-121212121212";

beforeEach(() => {
  toasts.success.mockClear();
  toasts.warning.mockClear();
  toasts.error.mockClear();
});

function file(overrides: Json = {}): Json {
  return {
    id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
    file_id: "ffffffff-ffff-4fff-8fff-fffffffffff1",
    filename: "handover notes.txt",
    content_type: "text/plain",
    size_bytes: 4096,
    state: "ready",
    attached_at: "2026-09-08T10:30:00Z",
    attached_by: "44444444-4444-4444-8444-444444444444",
    ...overrides,
  };
}

interface Scripted {
  status: number;
  body: Json | null;
}

/** A `fetch` routed by URL, because the panel fires two reads at mount. */
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

/** The two GETs the panel makes, answered from two lists. */
function panel(attached: Json[], attachable: Json[], onAttach?: (ids: string[]) => Scripted) {
  return fakeApi((url, init) => {
    if (url.includes("/attachable")) return { status: 200, body: { items: attachable } };
    if ((init.method ?? "GET") === "GET" && url.includes("/attachments")) {
      return { status: 200, body: { items: attached } };
    }
    if (init.method === "POST" && onAttach) {
      const sent = JSON.parse(String(init.body)) as { file_ids: string[] };
      return onAttach(sent.file_ids);
    }
    if (init.method === "DELETE") return { status: 204, body: null };
    return { status: 404, body: envelope("not_found", "Not found.") };
  });
}

describe("what the panel shows", () => {
  it("lists what is already shared and offers the rest of the basket", async () => {
    panel(
      [file()],
      [file({ file_id: "ffffffff-ffff-4fff-8fff-fffffffffff2", filename: "runbook.md" })],
    );
    renderWithQuery(<KtAttachments packageId={PACKAGE_ID} closed={false} />);

    expect(await screen.findByText("handover notes.txt")).toBeInTheDocument();
    expect(await screen.findByText("runbook.md")).toBeInTheDocument();
    // Only the offered one is choosable; the attached one already travelled.
    expect(screen.getAllByRole("checkbox")).toHaveLength(1);
  });

  it("says a stored file is stored, in the owner's own words", async () => {
    // The curator must not be told a recording is searchable either — nothing in this
    // deployment transcribes one, and the vocabulary is shared with the basket on
    // purpose so two consoles cannot describe one file differently.
    panel([file({ filename: "walkthrough.mp4", state: "stored" })], []);
    renderWithQuery(<KtAttachments packageId={PACKAGE_ID} closed={false} />);

    expect(await screen.findByText("Stored")).toBeInTheDocument();
    expect(screen.queryByText("Searchable")).not.toBeInTheDocument();
  });

  it("explains an empty picker rather than rendering a blank", async () => {
    // A curator holding `kt:manage` but not `basket:manage` gets an empty list by
    // design. A blank panel would read as broken; this one names the permission.
    panel([], []);
    renderWithQuery(<KtAttachments packageId={PACKAGE_ID} closed={false} />);

    expect(await screen.findByText("Nothing else to attach")).toBeInTheDocument();
    expect(screen.getByText(/Knowledge Basket management/)).toBeInTheDocument();
  });

  it("renders a refusal as a refusal", async () => {
    fakeApi(() => ({ status: 500, body: envelope("internal", "Something broke.", "req-9") }));
    renderWithQuery(<KtAttachments packageId={PACKAGE_ID} closed={false} />);

    expect(await screen.findByText("Something broke.")).toBeInTheDocument();
    expect(screen.getByText(/req-9/)).toBeInTheDocument();
  });
});

describe("attaching", () => {
  it("sends exactly the files that were ticked", async () => {
    const fetchMock = panel(
      [],
      [
        file({ file_id: "ffffffff-ffff-4fff-8fff-fffffffffff1", filename: "one.txt" }),
        file({ file_id: "ffffffff-ffff-4fff-8fff-fffffffffff2", filename: "two.txt" }),
      ],
      (ids) => ({ status: 201, body: { attached: ids.length } }),
    );
    renderWithQuery(<KtAttachments packageId={PACKAGE_ID} closed={false} />);
    await screen.findByText("two.txt");

    await userEvent.click(screen.getByRole("checkbox", { name: "Share two.txt" }));
    await userEvent.click(screen.getByRole("button", { name: "Share 1 file" }));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([, init]) => (init as RequestInit)?.method === "POST",
      );
      expect(post).toBeDefined();
      expect(JSON.parse(String((post![1] as RequestInit).body))).toEqual({
        file_ids: ["ffffffff-ffff-4fff-8fff-fffffffffff2"],
      });
    });
  });

  it("reports the server's count, not the number that was asked for", async () => {
    // Two ticked, one landed — because the other was already attached or is not this
    // employee's. The panel must say so rather than claim both.
    panel(
      [],
      [
        file({ file_id: "ffffffff-ffff-4fff-8fff-fffffffffff1", filename: "one.txt" }),
        file({ file_id: "ffffffff-ffff-4fff-8fff-fffffffffff2", filename: "two.txt" }),
      ],
      () => ({ status: 201, body: { attached: 1 } }),
    );
    renderWithQuery(<KtAttachments packageId={PACKAGE_ID} closed={false} />);
    await screen.findByText("two.txt");

    for (const box of screen.getAllByRole("checkbox")) {
      await userEvent.click(box);
    }
    await userEvent.click(screen.getByRole("button", { name: "Share 2 files" }));

    await waitFor(() => expect(toasts.warning).toHaveBeenCalled());
    expect(String(toasts.warning.mock.calls[0][0])).toContain("1 of 2 attached");
    expect(toasts.success).not.toHaveBeenCalled();
  });

  it("cannot be submitted with nothing chosen", async () => {
    panel([], [file()]);
    renderWithQuery(<KtAttachments packageId={PACKAGE_ID} closed={false} />);
    await screen.findByText("handover notes.txt");

    expect(screen.getByRole("button", { name: "Choose files to share" })).toBeDisabled();
  });
});

describe("a closed package", () => {
  it("offers no picker, because attaching would be refused", async () => {
    // A control that cannot succeed is the fake button §24 forbids.
    const fetchMock = panel([file()], []);
    renderWithQuery(<KtAttachments packageId={PACKAGE_ID} closed />);
    await screen.findByText("handover notes.txt");

    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(
      screen.getByText(/This package is closed, so no further files can be attached/),
    ).toBeInTheDocument();
    // And it does not even ask what could be attached.
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("/attachable")),
    ).toBe(false);
  });

  it("still allows withdrawing a file that is already shared", async () => {
    // Withdrawing something is never the act that needs blocking.
    const fetchMock = panel([file()], []);
    renderWithQuery(<KtAttachments packageId={PACKAGE_ID} closed />);
    await screen.findByText("handover notes.txt");

    await userEvent.click(
      screen.getByRole("button", { name: "Stop sharing handover notes.txt" }),
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => (init as RequestInit)?.method === "DELETE"),
      ).toBe(true),
    );
  });
});
