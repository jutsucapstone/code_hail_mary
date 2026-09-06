import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { describe, expect, it, vi } from "vitest";

import { KtCopilot } from "@/components/kt/kt-copilot";
import { KtShell } from "@/components/kt/kt-shell";
import {
  calledMethod,
  calledUrl,
  callIndexFor,
  envelope,
  scriptFetch,
  sentBody,
  type Json,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The KT copilot against a scripted API.
 *
 * What this file pins is the REQUESTS the browser makes — which URL, which method, and
 * exactly which fields — and how each documented response renders. The grounding gate,
 * the package window and the ACL are proven server-side; the point here is that the
 * frontend sends nothing that could widen them and renders their decisions faithfully.
 */

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/kt/KT-JUTSU-AAAA0001/ask",
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

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

function conversation(overrides: Json = {}): Json {
  return {
    id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    title: "Handover questions",
    message_count: 3,
    created_at: "2026-09-02T09:00:00Z",
    updated_at: "2026-09-02T09:30:00Z",
    ...overrides,
  };
}

function citation(overrides: Json = {}): Json {
  return {
    marker: 1,
    chunk_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    document_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    document_title: "Handover plan",
    source_system: "gmail",
    available: true,
    ...overrides,
  };
}

function message(overrides: Json = {}): Json {
  return {
    id: "mmmmmmmm-mmmm-4mmm-8mmm-mmmmmmmmmmm1",
    role: "user",
    content: "What should I understand first?",
    citations: [],
    insufficient_evidence: false,
    attempts: 0,
    created_at: "2026-09-02T09:00:00Z",
    ...overrides,
  };
}

function turn(overrides: Json = {}): Json {
  return {
    conversation_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    question_message_id: "11111111-1111-4111-8111-111111111111",
    answer_message_id: "22222222-2222-4222-8222-222222222222",
    answer: "Start with the handover plan [1].",
    insufficient_evidence: false,
    citations: [citation()],
    sources: [],
    attempts: 1,
    query_tokens: 12,
    ...overrides,
  };
}

const EMPTY_PAGE: Json = { items: [], next_cursor: null };

/** Every call whose URL contains `fragment`, for the page that makes the same request twice. */
function callIndexesFor(fetchMock: ReturnType<typeof scriptFetch>, fragment: string): number[] {
  return fetchMock.mock.calls
    .map((call, index) => (String(call[0]).includes(fragment) ? index : -1))
    .filter((index) => index >= 0);
}

function mount() {
  return renderWithQuery(
    <KtShell code={CODE}>
      <KtCopilot />
    </KtShell>,
  );
}

describe("the KT copilot", () => {
  it("renders the conversation list from the API, with titles and counts", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      {
        status: 200,
        body: {
          items: [
            conversation(),
            conversation({
              id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
              title: "Who owns the deploy pipeline?",
              message_count: 1,
            }),
          ],
          next_cursor: null,
        },
      },
    );
    mount();

    expect(await screen.findByText("Handover questions")).toBeInTheDocument();
    expect(screen.getByText("Who owns the deploy pipeline?")).toBeInTheDocument();
    expect(screen.getByText("3 messages")).toBeInTheDocument();
    expect(screen.getByText("1 message")).toBeInTheDocument();

    const list = callIndexFor(fetchMock, "/conversations");
    expect(calledUrl(fetchMock, list)).toBe(`${BASE}/conversations`);
    expect(calledMethod(fetchMock, list)).toBe("GET");
  });

  it("renders the honest empty state when there are no conversations", async () => {
    scriptFetch({ status: 200, body: recipientPackage() }, { status: 200, body: EMPTY_PAGE });
    mount();

    expect(await screen.findByText("No conversations yet")).toBeInTheDocument();
    expect(screen.getByText(/every conversation is kept here/i)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Suggested questions" })).toBeInTheDocument();
  });

  it("asks with exactly {question} and renders the answer, its marker and the citation", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: EMPTY_PAGE },
      { status: 200, body: turn() },
      // The list and the conversation are re-read after the turn is stored.
      {
        status: 200,
        body: {
          items: [
            conversation({
              id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
              title: "What should I understand first?",
              message_count: 2,
            }),
          ],
          next_cursor: null,
        },
      },
      {
        status: 200,
        body: {
          id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          title: "What should I understand first?",
          created_at: "2026-09-02T09:00:00Z",
          updated_at: "2026-09-02T09:00:05Z",
          messages: [
            message({ id: "11111111-1111-4111-8111-111111111111" }),
            message({
              id: "22222222-2222-4222-8222-222222222222",
              role: "assistant",
              content: "Start with the handover plan [1].",
              citations: [citation()],
              created_at: "2026-09-02T09:00:05Z",
            }),
          ],
        },
      },
    );
    mount();
    await screen.findByText("No conversations yet");

    await userEvent.type(screen.getByLabelText("Your question"), "What should I understand first?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText("Start with the handover plan [1].")).toBeInTheDocument();
    expect(screen.getByText("[1]")).toBeInTheDocument();
    expect(screen.getByText("Handover plan (gmail)")).toBeInTheDocument();

    const ask = callIndexFor(fetchMock, "/ask");
    expect(calledUrl(fetchMock, ask)).toBe(`${BASE}/ask`);
    expect(calledMethod(fetchMock, ask)).toBe("POST");
    // Exactly one field. No conversation_id before one exists, no k, nothing that names
    // a tenant, a person, a filter or a model.
    expect(sentBody(fetchMock, ask)).toEqual({ question: "What should I understand first?" });

    // The input is cleared for the follow-up; the suggestions give way to the thread.
    expect(screen.getByLabelText("Your question")).toHaveValue("");
    expect(screen.queryByRole("group", { name: "Suggested questions" })).not.toBeInTheDocument();
  });

  it("sends conversation_id on a follow-up inside the same conversation", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: EMPTY_PAGE },
      { status: 200, body: turn() },
      { status: 200, body: EMPTY_PAGE },
      { status: 200, body: { ...conversation(), id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", messages: [] } },
      {
        status: 200,
        body: turn({
          question_message_id: "33333333-3333-4333-8333-333333333333",
          answer_message_id: "44444444-4444-4444-8444-444444444444",
          answer: "Nothing further is recorded about that [1].",
        }),
      },
      { status: 200, body: EMPTY_PAGE },
      { status: 200, body: { ...conversation(), id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", messages: [] } },
    );
    mount();
    await screen.findByText("No conversations yet");

    await userEvent.type(screen.getByLabelText("Your question"), "What should I understand first?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Start with the handover plan [1].");

    await userEvent.type(screen.getByLabelText("Your question"), "And after that?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Nothing further is recorded about that [1].");

    const asks = callIndexesFor(fetchMock, "/ask");
    expect(asks).toHaveLength(2);
    expect(sentBody(fetchMock, asks[0])).toEqual({ question: "What should I understand first?" });
    expect(sentBody(fetchMock, asks[1])).toEqual({
      question: "And after that?",
      conversation_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    });
    // Both turns stay on screen, oldest first.
    expect(screen.getByText("Start with the handover plan [1].")).toBeInTheDocument();
  });

  it("lets the stored conversation replace the overlaid turn by message id", async () => {
    // After an ask, the turn the copilot returned is shown at once and the conversation is
    // re-read; once the re-read carries the same message ids, the stored copy is the one
    // rendered and the overlay is dropped. The stored text differs here only so the test
    // can tell which copy is on screen — the ids are the contract.
    scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: EMPTY_PAGE },
      { status: 200, body: turn() },
      {
        status: 200,
        body: {
          items: [conversation({ id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", message_count: 2 })],
          next_cursor: null,
        },
      },
      {
        status: 200,
        body: {
          id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          title: "What should I understand first?",
          created_at: "2026-09-02T09:00:00Z",
          updated_at: "2026-09-02T09:00:05Z",
          messages: [
            message({ id: "11111111-1111-4111-8111-111111111111", content: "What should I understand first? (stored)" }),
            message({
              id: "22222222-2222-4222-8222-222222222222",
              role: "assistant",
              content: "Start with the handover plan [1]. (stored)",
              citations: [citation()],
              created_at: "2026-09-02T09:00:05Z",
            }),
          ],
        },
      },
    );
    mount();
    await screen.findByText("No conversations yet");

    await userEvent.type(screen.getByLabelText("Your question"), "What should I understand first?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText("Start with the handover plan [1]. (stored)")).toBeInTheDocument();
    expect(screen.queryByText("Start with the handover plan [1].")).not.toBeInTheDocument();
    expect(screen.getByText("What should I understand first? (stored)")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Save this answer:/ })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /^Save as a question:/ })).toHaveLength(1);
  });

  it("renders an unavailable citation without a button, and fetches the masked span for an available one", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [conversation()], next_cursor: null } },
      {
        status: 200,
        body: {
          ...conversation(),
          messages: [
            message(),
            message({
              id: "mmmmmmmm-mmmm-4mmm-8mmm-mmmmmmmmmmm2",
              role: "assistant",
              content: "Two sources speak to this [1][2].",
              citations: [
                citation(),
                citation({
                  marker: 2,
                  chunk_id: "99999999-9999-4999-8999-999999999999",
                  document_title: "Old runbook",
                  available: false,
                }),
              ],
              created_at: "2026-09-02T09:00:05Z",
            }),
          ],
        },
      },
      {
        status: 200,
        body: {
          chunk_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
          document_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
          document_title: "Handover plan",
          source_system: "gmail",
          occurred_at: "2026-08-01T00:00:00Z",
          char_start: 120,
          char_end: 260,
          text: "Send the plan to [EMAIL_A7] before the first review.",
        },
      },
    );
    mount();

    await userEvent.click(
      await screen.findByRole("button", { name: "Open conversation: Handover questions" }),
    );
    expect(await screen.findByText("Two sources speak to this [1][2].")).toBeInTheDocument();

    // The unavailable one: its label and marker stay, its button does not exist.
    expect(screen.getByText("Old runbook (gmail)")).toBeInTheDocument();
    expect(screen.getByText("No longer available to you")).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: /view source/i });
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveAccessibleName("View source for [1] Handover plan");

    await userEvent.click(buttons[0]);

    // The masked text is rendered whole. Never sliced with char_start/char_end.
    expect(
      await screen.findByText("Send the plan to [EMAIL_A7] before the first review."),
    ).toBeInTheDocument();
    const evidence = callIndexFor(fetchMock, "/evidence/");
    expect(calledUrl(fetchMock, evidence)).toBe(
      "/api/jutsu/v1/evidence/cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    );
    expect(calledMethod(fetchMock, evidence)).toBe("GET");
  });

  it("renders the refusal sentence for insufficient evidence", async () => {
    scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: EMPTY_PAGE },
      { status: 200, body: turn({ answer: null, insufficient_evidence: true, citations: [] }) },
      { status: 200, body: EMPTY_PAGE },
      { status: 200, body: { ...conversation(), id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", messages: [] } },
    );
    mount();
    await screen.findByText("No conversations yet");

    await userEvent.click(screen.getByRole("button", { name: "What is still unfinished?" }));

    expect(
      await screen.findByText(
        "The evidence you are authorised to read does not answer this. JUTSU refuses rather than guesses.",
      ),
    ).toBeInTheDocument();
  });

  it("renders the not-configured notice for a 503 and never the corpus-wide evidence search", async () => {
    scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: EMPTY_PAGE },
      {
        status: 503,
        body: envelope("unavailable", "Answer synthesis is not configured for this deployment."),
      },
    );
    mount();
    await screen.findByText("No conversations yet");

    await userEvent.type(screen.getByLabelText("Your question"), "What should I understand first?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText(/answering in prose is not configured/i)).toBeInTheDocument();
    expect(screen.getByText(/knowledge tabs/i)).toBeInTheDocument();
    // Not EvidenceSearch: that would search outside the package window.
    expect(screen.queryByLabelText(/search query/i)).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/search the corpus/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Your question")).not.toBeInTheDocument();
  });

  it("renders a 429 with the server's message and no retry", async () => {
    scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: EMPTY_PAGE },
      {
        status: 429,
        body: envelope("rate_limited", "Your question budget for this hour is spent."),
      },
    );
    mount();
    await screen.findByText("No conversations yet");

    await userEvent.type(screen.getByLabelText("Your question"), "What should I understand first?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Your question budget for this hour is spent.")).toBeInTheDocument();
    expect(within(alert).queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
  });

  it("archives a conversation after confirmation", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [conversation()], next_cursor: null } },
      { status: 204, body: null },
      { status: 200, body: EMPTY_PAGE },
    );
    mount();
    await screen.findByText("Handover questions");

    await userEvent.click(screen.getByRole("button", { name: "Archive Handover questions" }));
    // The first click only asks; nothing has been sent yet.
    expect(callIndexesFor(fetchMock, "/archive")).toHaveLength(0);

    await userEvent.click(
      screen.getByRole("button", { name: "Confirm archive of Handover questions" }),
    );

    await waitFor(() => expect(callIndexesFor(fetchMock, "/archive")).toHaveLength(1));
    const archive = callIndexFor(fetchMock, "/archive");
    expect(calledUrl(fetchMock, archive)).toBe(
      `${BASE}/conversations/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/archive`,
    );
    expect(calledMethod(fetchMock, archive)).toBe("POST");
    expect(await screen.findByText("No conversations yet")).toBeInTheDocument();
  });

  it("saves an answer as a message bookmark", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [conversation()], next_cursor: null } },
      {
        status: 200,
        body: {
          ...conversation(),
          messages: [
            message(),
            message({
              id: "mmmmmmmm-mmmm-4mmm-8mmm-mmmmmmmmmmm2",
              role: "assistant",
              content: "Start with the handover plan [1].",
              citations: [citation()],
              created_at: "2026-09-02T09:00:05Z",
            }),
          ],
        },
      },
      {
        status: 201,
        body: {
          id: "77777777-7777-4777-8777-777777777777",
          kind: "message",
          ref_id: "mmmmmmmm-mmmm-4mmm-8mmm-mmmmmmmmmmm2",
          note: null,
          label: "Start with the handover plan [1].",
          tab: "ask",
          available: true,
          created_at: "2026-09-02T10:00:00Z",
          updated_at: "2026-09-02T10:00:00Z",
        },
      },
    );
    mount();

    await userEvent.click(
      await screen.findByRole("button", { name: "Open conversation: Handover questions" }),
    );
    await screen.findByText("Start with the handover plan [1].");

    await userEvent.click(
      screen.getByRole("button", { name: "Save this answer: Start with the handover plan [1]." }),
    );

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Saved to your items."));
    const bookmark = callIndexFor(fetchMock, "/bookmarks");
    expect(calledUrl(fetchMock, bookmark)).toBe(`${BASE}/bookmarks`);
    expect(calledMethod(fetchMock, bookmark)).toBe("POST");
    expect(sentBody(fetchMock, bookmark)).toEqual({
      kind: "message",
      ref_id: "mmmmmmmm-mmmm-4mmm-8mmm-mmmmmmmmmmm2",
    });
  });

  it("searches conversations with a POST so the words stay out of the URL", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [conversation()], next_cursor: null } },
      {
        status: 200,
        body: {
          items: [
            conversation({
              id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
              title: "Deploy pipeline ownership",
              message_count: 2,
            }),
          ],
          next_cursor: null,
        },
      },
    );
    mount();
    await screen.findByText("Handover questions");

    await userEvent.type(screen.getByLabelText("Search your conversations"), "deploy pipeline");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("Deploy pipeline ownership")).toBeInTheDocument();
    expect(screen.queryByText("Handover questions")).not.toBeInTheDocument();

    const search = callIndexFor(fetchMock, "/conversations/search");
    expect(calledUrl(fetchMock, search)).toBe(`${BASE}/conversations/search`);
    expect(calledMethod(fetchMock, search)).toBe("POST");
    expect(sentBody(fetchMock, search)).toEqual({ q: "deploy pipeline" });

    await userEvent.click(screen.getByRole("button", { name: /clear search/i }));
    expect(await screen.findByText("Handover questions")).toBeInTheDocument();
  });
});
