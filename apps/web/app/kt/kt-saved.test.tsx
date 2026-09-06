import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "sonner";

import { KtSaved } from "@/components/kt/kt-saved";
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
 * The Saved tab, against a scripted API.
 *
 * What this file pins: the list groups by kind and omits empty groups, an item the
 * server marks unavailable is a labelled stub with no link, Remove and the question form
 * send exactly the request the API documents, and every documented state renders. The
 * ACL over each referent — what makes `available` true or false — is proven server-side
 * in test_kt.py; the frontend only renders that decision, never softens it.
 */

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/kt/KT-JUTSU-AAAA0001/saved",
}));

// Toast text renders inside the app-level <Toaster>, which these component tests do
// not mount — assert the call, not the portal.
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

beforeEach(() => {
  vi.clearAllMocks();
});

const CODE = "KT-JUTSU-AAAA0001";
const BASE = `/api/jutsu/v1/kt/${CODE}`;

function recipientPackage(overrides: Json = {}): Json {
  return {
    kt_code: CODE,
    status: "claimed",
    scope: ["documents", "profile"],
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

function bookmark(overrides: Json = {}): Json {
  return {
    id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    kind: "claim",
    ref_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    label: "Move the ledger to Postgres",
    note: null,
    tab: "decisions",
    available: true,
    created_at: "2026-09-02T10:00:00Z",
    updated_at: "2026-09-02T10:00:00Z",
    ...overrides,
  };
}

function mount() {
  return renderWithQuery(
    <KtShell code={CODE}>
      <KtSaved />
    </KtShell>,
  );
}

describe("the KT saved tab", () => {
  it("groups items by kind, omits empty groups and shows an unavailable item as a stub", async () => {
    scriptFetch(
      { status: 200, body: recipientPackage() },
      {
        status: 200,
        body: {
          items: [
            bookmark(),
            bookmark({
              id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
              kind: "document",
              label: "Q3 retro notes",
              note: "Read before the first sync.",
              tab: "documents",
              available: false,
            }),
            bookmark({
              id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
              kind: "question",
              ref_id: null,
              label: "Who owns the on-call rota now?",
              note: "Who owns the on-call rota now?",
              tab: null,
            }),
          ],
        },
      },
    );
    mount();

    expect(await screen.findByRole("heading", { name: "Claims" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Documents" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Questions" })).toBeInTheDocument();
    // No message bookmark was returned, so there is no Answers group.
    expect(screen.queryByRole("heading", { name: "Answers" })).not.toBeInTheDocument();

    // The available claim links into its tab.
    expect(screen.getByRole("link", { name: "Open: Move the ledger to Postgres" })).toHaveAttribute(
      "href",
      `/kt/${CODE}/decisions`,
    );

    // The unavailable document: the pill, its note, and NO link anywhere near it.
    const stub = screen.getByText("Q3 retro notes").closest("li");
    expect(stub).not.toBeNull();
    expect(within(stub!).getByText("No longer available to you")).toBeInTheDocument();
    expect(within(stub!).getByText("Read before the first sync.")).toBeInTheDocument();
    expect(within(stub!).queryByRole("link")).not.toBeInTheDocument();
    expect(
      within(stub!).getByRole("button", { name: "Remove saved item: Q3 retro notes" }),
    ).toBeInTheDocument();

    // A question's note IS its label; it is not printed twice.
    expect(screen.getAllByText("Who owns the on-call rota now?")).toHaveLength(1);
  });

  it("removes an item with DELETE /bookmarks/{id} and refetches", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [bookmark()] } },
      { status: 204, body: null },
      { status: 200, body: { items: [] } },
    );
    mount();

    await userEvent.click(
      await screen.findByRole("button", { name: "Remove saved item: Move the ledger to Postgres" }),
    );

    const remove = callIndexFor(fetchMock, "/bookmarks/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
    expect(calledUrl(fetchMock, remove)).toBe(`${BASE}/bookmarks/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`);
    expect(calledMethod(fetchMock, remove)).toBe("DELETE");
    expect(await screen.findByText("Nothing saved yet")).toBeInTheDocument();
    expect(vi.mocked(toast.success)).toHaveBeenCalledWith("Removed from your items.");
  });

  it("saves a question with POST {kind: question, note} and clears the field", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [] } },
      {
        status: 201,
        body: bookmark({
          id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
          kind: "question",
          ref_id: null,
          label: "Where is the runbook?",
          note: "Where is the runbook?",
          tab: null,
        }),
      },
      {
        status: 200,
        body: {
          items: [
            bookmark({
              id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
              kind: "question",
              ref_id: null,
              label: "Where is the runbook?",
              note: "Where is the runbook?",
              tab: null,
            }),
          ],
        },
      },
    );
    mount();

    const field = await screen.findByLabelText("Save a question");
    expect(field).toHaveAttribute("id", "kt-saved-question");
    expect(field).toHaveAttribute("maxlength", "2000");
    // Nothing typed, nothing to send.
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

    await userEvent.type(field, "Where is the runbook?");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    const post = fetchMock.mock.calls.findIndex(
      (call) => String(call[0]) === `${BASE}/bookmarks` && (call[1] as RequestInit).method === "POST",
    );
    expect(post).toBeGreaterThan(-1);
    expect(sentBody(fetchMock, post)).toEqual({ kind: "question", note: "Where is the runbook?" });

    expect(await screen.findByRole("heading", { name: "Questions" })).toBeInTheDocument();
    expect(screen.getByText("Where is the runbook?")).toBeInTheDocument();
    expect(field).toHaveValue("");
    expect(vi.mocked(toast.success)).toHaveBeenCalledWith("Saved.");
  });

  it("renders the honest empty state when nothing has been saved", async () => {
    scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [] } },
    );
    mount();

    expect(await screen.findByText("Nothing saved yet")).toBeInTheDocument();
    expect(
      screen.getByText(/save a claim or document from any tab, an answer from ask kt/i),
    ).toBeInTheDocument();
  });

  it("announces the loading state while the list is in flight", async () => {
    const fetchMock = scriptFetch({ status: 200, body: recipientPackage() });
    fetchMock.mockReturnValueOnce(new Promise(() => {}));
    mount();

    expect(await screen.findByText("Loading your saved items.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("renders a throttled list as a retryable failure with the server's sentence", async () => {
    scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 429, body: envelope("rate_limited", "Too many requests. Try again in a minute.") },
    );
    mount();

    expect(await screen.findByText("Too many requests. Try again in a minute.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("surfaces a failed save as a toast and keeps the draft", async () => {
    scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [] } },
      { status: 403, body: envelope("permission_denied", "This Knowledge Transfer package has been revoked.") },
    );
    mount();

    const field = await screen.findByLabelText("Save a question");
    await userEvent.type(field, "Still here?");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
        "This Knowledge Transfer package has been revoked.",
      ),
    );
    expect(field).toHaveValue("Still here?");
  });
});
