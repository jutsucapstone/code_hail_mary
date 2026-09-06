import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { describe, expect, it, vi } from "vitest";

import LearnPage from "@/app/kt/[code]/learn/page";
import { KtOverview } from "@/components/kt/kt-pages";
import { KtShell } from "@/components/kt/kt-shell";
import {
  callIndexFor,
  calledMethod,
  envelope,
  routeFetch,
  sentBody,
  type Json,
  type RoutedResponse,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The personalised workspace, against a scripted API.
 *
 * What this file pins is what the recipient SEES for each documented workspace shape and
 * what the browser SENDS when they mark progress — never the computation itself, which
 * test_kt_workspace.py proves against real Postgres. Two things matter most here: an
 * unreliable coverage figure renders its reason and no percentage anywhere (§36, rule
 * 8), and every write goes to `/progress/{key}` with the body the API documents.
 */

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/kt/KT-JUTSU-AAAA0001",
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const CODE = "KT-JUTSU-AAAA0001";

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
      role_title: "Staff Engineer",
      role_level: "L6",
    },
    ...overrides,
  };
}

function reliableCoverage(overrides: Json = {}): Json {
  return {
    categories: [
      { category: "decisions", claim_type: "decision", claims_visible: 3 },
      { category: "people", claim_type: "person", claims_visible: 2 },
    ],
    chunks_covered: 50,
    chunks_total: 100,
    documents_extracted: 4,
    documents_visible: 6,
    extraction_ratio: 0.5,
    reliable: true,
    reason:
      "Computed from the documents your account may read inside this package's window and the latest extraction run over each of them.",
    ...overrides,
  };
}

const UNRELIABLE_REASON =
  "Coverage cannot be calculated reliably yet: extraction has not run over the documents you can read.";

function unreliableCoverage(): Json {
  return reliableCoverage({
    chunks_covered: 0,
    chunks_total: 0,
    documents_extracted: 0,
    extraction_ratio: null,
    reliable: false,
    reason: UNRELIABLE_REASON,
    categories: [{ category: "decisions", claim_type: "decision", claims_visible: 0 }],
  });
}

const CLAIM_KEY = "claim:11111111-1111-4111-8111-111111111111";
const DOC_KEY = "document:22222222-2222-4222-8222-222222222222";

function workspace(overrides: Json = {}): Json {
  return {
    coverage: reliableCoverage(),
    learning_path: [
      {
        day: 1,
        title: "Understand your responsibility",
        items: [
          {
            key: "step:profile",
            kind: "step",
            label: "Who you are taking over from",
            why: "Their practice, title and level as the organisation records them.",
            tab: "",
            ref_id: null,
            state: "done",
          },
          {
            key: CLAIM_KEY,
            kind: "decision",
            label: "Adopt pgvector for retrieval",
            why: "Decided 2026-08-14 · Architecture review",
            tab: "decisions",
            ref_id: "11111111-1111-4111-8111-111111111111",
            state: null,
          },
        ],
      },
      {
        day: 30,
        title: "Read the source material",
        items: [
          {
            key: DOC_KEY,
            kind: "document",
            label: "Handover plan",
            why: "gmail · 2026-08-01",
            tab: "documents",
            ref_id: "22222222-2222-4222-8222-222222222222",
            state: "unclear",
          },
        ],
      },
    ],
    recommendations: [
      {
        key: CLAIM_KEY,
        kind: "path",
        label: "Adopt pgvector for retrieval",
        why: "Next on your learning path.",
        tab: "decisions",
        ref_id: "11111111-1111-4111-8111-111111111111",
      },
    ],
    gaps: [
      {
        key: "category:meetings",
        label: "No meetings evidence is visible to you yet",
        why: "Nothing extracted in this window is readable by your account.",
        source: "evidence",
        tab: "meetings",
        ref_id: null,
      },
      {
        key: DOC_KEY,
        label: "Handover plan",
        why: "You marked this unclear.",
        source: "you",
        tab: "documents",
        ref_id: "22222222-2222-4222-8222-222222222222",
      },
    ],
    resume: {
      last_conversation: {
        id: "33333333-3333-4333-8333-333333333333",
        title: "How was the retrieval index chosen?",
        message_count: 4,
        created_at: "2026-09-02T09:00:00Z",
        updated_at: "2026-09-02T09:10:00Z",
      },
      last_activity_at: "2026-09-02T09:10:00Z",
      bookmarks: 2,
      unclear: 1,
      path_done: 1,
      path_total: 3,
    },
    ...overrides,
  };
}

function firstVisit(): Json {
  return {
    last_conversation: null,
    last_activity_at: null,
    bookmarks: 0,
    unclear: 0,
    path_done: 0,
    path_total: 3,
  };
}

const CLAIM: RoutedResponse = { match: "/kt/claim", status: 200, body: recipientPackage() };

function ws(body: Json): RoutedResponse {
  return { match: "/workspace", status: 200, body };
}

function progress(key: string, status: number, body: Json | null): RoutedResponse {
  return { match: `/progress/${encodeURIComponent(key)}`, status, body };
}

function renderOverview(...responses: RoutedResponse[]) {
  const fetchMock = routeFetch(CLAIM, ...responses);
  renderWithQuery(
    <KtShell code={CODE}>
      <KtOverview />
    </KtShell>,
  );
  return fetchMock;
}

function renderLearn(...responses: RoutedResponse[]) {
  const fetchMock = routeFetch(CLAIM, ...responses);
  renderWithQuery(
    <KtShell code={CODE}>
      <LearnPage />
    </KtShell>,
  );
  return fetchMock;
}

describe("the workspace resume card", () => {
  it("welcomes a returning recipient with their last conversation and progress", async () => {
    renderOverview(ws(workspace()));

    expect(await screen.findByRole("heading", { name: "Welcome back." })).toBeInTheDocument();
    expect(screen.getByText("Continue where you left off.")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "How was the retrieval index chosen?" }),
    ).toHaveAttribute(
      "href",
      `/kt/${CODE}/ask?conversation=33333333-3333-4333-8333-333333333333`,
    );
    expect(screen.getByText("1 of 3 learning steps done")).toBeInTheDocument();
    expect(screen.getByText("1 still unclear")).toBeInTheDocument();
    expect(screen.getByText("2 saved")).toBeInTheDocument();
  });

  it("introduces the package on a first visit, naming the subject and their role", async () => {
    renderOverview(ws(workspace({ resume: firstVisit() })));

    expect(
      await screen.findByRole("heading", { name: "Your knowledge transfer" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/taking over from/).textContent).toBe(
      "You're taking over from Grace Hopper · Staff Engineer · L6",
    );
    expect(screen.queryByText("Welcome back.")).not.toBeInTheDocument();
  });
});

describe("the coverage panel", () => {
  it("renders counts and, for a reliable figure, the extraction percentage", async () => {
    renderOverview(ws(workspace()));

    expect(await screen.findByText("3 decisions you can read")).toBeInTheDocument();
    expect(screen.getByText("2 people you can read")).toBeInTheDocument();
    expect(
      screen.getByText("6 documents in the window you can read, 4 of them extracted"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Extraction has covered 50% of the text you can read."),
    ).toBeInTheDocument();
  });

  it("renders the backend's reason and no percentage at all when coverage is unreliable", async () => {
    renderOverview(ws(workspace({ coverage: unreliableCoverage() })));

    expect(await screen.findByText(UNRELIABLE_REASON)).toBeInTheDocument();
    expect(screen.getByText("0 decisions you can read")).toBeInTheDocument();
    // Rule 8: a figure the backend refused to compute is never derived in the browser.
    expect(document.body.textContent).not.toContain("%");
    expect(screen.queryByText(/extraction has covered/i)).not.toBeInTheDocument();
  });
});

describe("recommendations", () => {
  it("renders each label as a link to its tab, with the reason beneath", async () => {
    renderOverview(ws(workspace()));

    const heading = await screen.findByRole("heading", { name: "Recommended next" });
    const section = heading.closest("section") as HTMLElement;
    expect(
      within(section).getByRole("link", { name: "Adopt pgvector for retrieval" }),
    ).toHaveAttribute("href", `/kt/${CODE}/decisions`);
    expect(within(section).getByText("Next on your learning path.")).toBeInTheDocument();
  });

  it("says why there is nothing when the list is empty", async () => {
    renderOverview(ws(workspace({ recommendations: [] })));

    expect(await screen.findByText("Nothing to recommend yet")).toBeInTheDocument();
    expect(
      screen.getByText(/recommendations come from the evidence you can read/i),
    ).toBeInTheDocument();
  });
});

describe("still unclear", () => {
  it("lists what the recipient marked before what the evidence lacks, and clears a mark", async () => {
    const fetchMock = renderOverview(
      ws(workspace()),
      progress(DOC_KEY, 204, null),
      // The refetch the write triggers: the mark is gone server-side.
      ws(workspace({ gaps: [] })),
    );

    const heading = await screen.findByRole("heading", { name: "Still unclear" });
    const section = heading.closest("section") as HTMLElement;
    const groups = within(section).getAllByRole("heading", { level: 3 });
    expect(groups.map((h) => h.textContent)).toEqual([
      "You marked this unclear",
      "Missing from the evidence",
    ]);
    expect(within(section).getByText("You marked this unclear.")).toBeInTheDocument();
    expect(
      within(section).getByRole("link", { name: "No meetings evidence is visible to you yet" }),
    ).toHaveAttribute("href", `/kt/${CODE}/meetings`);

    await userEvent.click(within(section).getByRole("button", { name: /mark understood/i }));

    await waitFor(() => {
      const index = callIndexFor(fetchMock, `/progress/${encodeURIComponent(DOC_KEY)}`);
      expect(calledMethod(fetchMock, index)).toBe("DELETE");
    });
    expect(
      await screen.findByText(
        "Nothing is marked unclear, and every category in scope has evidence you can read.",
      ),
    ).toBeInTheDocument();
  });

  it("surfaces a spent budget on a write as a message, not a silent no-op", async () => {
    renderOverview(
      ws(workspace()),
      progress(DOC_KEY, 429, envelope("rate_limited", "Too many changes. Try again shortly.")),
    );

    await screen.findByRole("heading", { name: "Still unclear" });
    await userEvent.click(screen.getByRole("button", { name: /mark understood/i }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Too many changes. Try again shortly."),
    );
  });
});

describe("the learning path", () => {
  it("summarises each stage on the overview and links to the full path", async () => {
    renderOverview(ws(workspace()));

    await screen.findByRole("heading", { name: "Your learning path" });
    expect(screen.getByText("Day 1 · Understand your responsibility")).toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();
    expect(screen.getByText("Day 30 · Read the source material")).toBeInTheDocument();
    expect(screen.getByText("0/1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the full path" })).toHaveAttribute(
      "href",
      `/kt/${CODE}/learn`,
    );
    // The overview summarises; the per-item controls live on the learn page.
    expect(screen.queryByRole("button", { name: /mark done/i })).not.toBeInTheDocument();
  });

  it("marks a step done from the learn page with the documented body", async () => {
    const fetchMock = renderLearn(
      ws(workspace()),
      progress(CLAIM_KEY, 200, {
        item_key: CLAIM_KEY,
        state: "done",
        updated_at: "2026-09-06T00:00:00Z",
      }),
      ws(workspace()),
    );

    expect(
      await screen.findByRole("heading", { name: "Day 1 — Understand your responsibility" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/^done$/i)).toBeInTheDocument();
    expect(screen.getByText(/^unclear$/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Adopt pgvector for retrieval" })).toHaveAttribute(
      "href",
      `/kt/${CODE}/decisions`,
    );
    expect(screen.getByRole("link", { name: "Who you are taking over from" })).toHaveAttribute(
      "href",
      `/kt/${CODE}`,
    );
    // Clear appears only where a state exists: two of the three items carry one.
    expect(screen.getAllByRole("button", { name: /^clear:/i })).toHaveLength(2);

    await userEvent.click(
      screen.getByRole("button", { name: "Mark done: Adopt pgvector for retrieval" }),
    );

    await waitFor(() => {
      const index = callIndexFor(fetchMock, `/progress/${encodeURIComponent(CLAIM_KEY)}`);
      expect(calledMethod(fetchMock, index)).toBe("PUT");
      expect(sentBody(fetchMock, index)).toEqual({ state: "done" });
    });
  });

  it("sends unclear for Still unclear and DELETEs for Clear", async () => {
    const fetchMock = renderLearn(
      ws(workspace()),
      progress(CLAIM_KEY, 200, {
        item_key: CLAIM_KEY,
        state: "unclear",
        updated_at: "2026-09-06T00:00:00Z",
      }),
      ws(workspace()),
      progress(DOC_KEY, 204, null),
      ws(workspace()),
    );
    await screen.findByRole("heading", { name: "Day 1 — Understand your responsibility" });

    await userEvent.click(
      screen.getByRole("button", { name: "Still unclear: Adopt pgvector for retrieval" }),
    );
    await waitFor(() => {
      const index = callIndexFor(fetchMock, `/progress/${encodeURIComponent(CLAIM_KEY)}`);
      expect(calledMethod(fetchMock, index)).toBe("PUT");
      expect(sentBody(fetchMock, index)).toEqual({ state: "unclear" });
    });

    await userEvent.click(screen.getByRole("button", { name: "Clear: Handover plan" }));
    await waitFor(() => {
      const index = callIndexFor(fetchMock, `/progress/${encodeURIComponent(DOC_KEY)}`);
      expect(calledMethod(fetchMock, index)).toBe("DELETE");
    });
  });

  it("explains an empty path with the coverage reason instead of inventing steps", async () => {
    renderLearn(
      ws(
        workspace({
          learning_path: [],
          coverage: unreliableCoverage(),
          resume: firstVisit(),
        }),
      ),
    );

    expect(await screen.findByText("Not enough evidence yet")).toBeInTheDocument();
    expect(screen.getByText(UNRELIABLE_REASON)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /mark done/i })).not.toBeInTheDocument();
  });

  it("explains an empty path under reliable coverage without borrowing the coverage sentence", async () => {
    // Reliable coverage means extraction ran over readable documents; if the path is still
    // empty, the categories in scope produced nothing, and the coverage sentence would
    // describe a computation rather than the absence.
    renderLearn(ws(workspace({ learning_path: [], resume: firstVisit() })));

    expect(await screen.findByText("Not enough evidence yet")).toBeInTheDocument();
    expect(
      screen.getByText(/nothing in this package's scope has produced a step yet/i),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("%");
  });
});

describe("the workspace region's states", () => {
  it("announces loading and still renders the package facts beneath", async () => {
    renderOverview({ match: "/workspace", status: 200, body: null, pending: true });

    expect(await screen.findByText("Loading your workspace.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "About this package" })).toBeInTheDocument();
    expect(screen.queryByText("Knowledge coverage")).not.toBeInTheDocument();
  });

  it("renders a 503 as a retryable failure without hiding the package facts", async () => {
    renderOverview({
      match: "/workspace",
      status: 503,
      body: envelope("unavailable", "The graph is not reachable right now."),
    });

    expect(await screen.findByText("The graph is not reachable right now.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "About this package" })).toBeInTheDocument();
    expect(screen.queryByText("Knowledge coverage")).not.toBeInTheDocument();
  });

  it("renders a 403 as the package's own refusal, never as a role problem", async () => {
    // Every KT route re-runs the package's authorization first, so a 403 here is the
    // revoked/expired sentence the server chose — the shell's KtRefusal wording, not
    // FailureState's "your role does not include…".
    renderOverview({
      match: "/workspace",
      status: 403,
      body: envelope("kt_revoked", "This knowledge-transfer package has been revoked."),
    });

    expect(await screen.findByText("This package is closed")).toBeInTheDocument();
    expect(
      screen.getByText("This knowledge-transfer package has been revoked."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/your role does not include/i)).not.toBeInTheDocument();
  });
});
