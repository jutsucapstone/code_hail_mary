import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import KnowledgeTransferEntryPage from "@/app/(product)/handover/page";
import { KtDocuments } from "@/components/kt/kt-pages";
import { KtShell } from "@/components/kt/kt-shell";
import {
  calledMethod,
  calledUrl,
  callIndexFor,
  envelope,
  routeFetch,
  scriptFetch,
  sentBody,
  type Json,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The KT entry and the console shell, against a scripted API.
 *
 * The §47 KT matrix's frontend half: a valid ID navigates into the workspace, a revoked
 * or expired package renders the SERVER'S sentence verbatim, and an unknown ID renders
 * the uniform not-found. The authorization itself — binding, expiry, revocation, ACL —
 * is proven in test_kt.py against real Postgres; what this file pins is that the
 * frontend faithfully renders those decisions and never softens them.
 */

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/kt/KT-JUTSU-AAAA0001",
}));

function recipientPackage(overrides: Json = {}): Json {
  return {
    kt_code: "KT-JUTSU-AAAA0001",
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

describe("the KT entry page", () => {
  // The page reads the session's organisation on mount, so every test here routes by URL:
  // positional scripting would hand the claim's answer to whichever request asked first.
  const ORGANISATION = { match: "/v1/me/organisation", status: 200, body: { name: "Example Analytical" } };

  it("claims through the API and navigates into the workspace", async () => {
    const fetchMock = routeFetch(ORGANISATION, {
      match: "/v1/kt/claim",
      status: 200,
      body: recipientPackage(),
    });
    renderWithQuery(<KnowledgeTransferEntryPage />);

    await userEvent.type(screen.getByLabelText(/kt id/i), "KT-JUTSU-AAAA0001");
    await userEvent.click(screen.getByRole("button", { name: /open kt/i }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/kt/KT-JUTSU-AAAA0001"));
    const claim = callIndexFor(fetchMock, "/v1/kt/claim");
    expect(calledUrl(fetchMock, claim)).toBe("/api/jutsu/v1/kt/claim");
    expect(calledMethod(fetchMock, claim)).toBe("POST");
    expect(sentBody(fetchMock, claim)).toEqual({ kt_code: "KT-JUTSU-AAAA0001" });
  });

  it("names the organisation the session is signed into", async () => {
    // The production "B cannot open A's package" was a session in another organisation,
    // where the ID does not exist, and nothing on this page said which one it was.
    routeFetch(ORGANISATION);
    renderWithQuery(<KnowledgeTransferEntryPage />);

    expect(await screen.findByText("Example Analytical")).toBeInTheDocument();
    expect(
      screen.getByText(/opens only inside the organisation that issued it/i),
    ).toBeInTheDocument();
  });

  it("renders the server's refusal for an unknown ID", async () => {
    // No organisation route here: an unreadable name hides the line and never the form.
    routeFetch({
      match: "/v1/kt/claim",
      status: 404,
      body: envelope("not_found", "No package matches that ID. Check it with your administrator."),
    });
    renderWithQuery(<KnowledgeTransferEntryPage />);

    await userEvent.type(screen.getByLabelText(/kt id/i), "KT-JUTSU-WRONG000");
    await userEvent.click(screen.getByRole("button", { name: /open kt/i }));

    expect(await screen.findByText(/no package matches that id/i)).toBeInTheDocument();
    expect(screen.queryByText(/signed in to/i)).not.toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("sends only the trimmed ID and renders a closed package's own sentence", async () => {
    // The door takes a code and nothing else — no user id, no subject — and a completed,
    // revoked or expired package answers with the server's sentence, never a softer one.
    const sentence =
      "This Knowledge Transfer is complete. Ask your administrator if you need it reopened.";
    const fetchMock = routeFetch(ORGANISATION, {
      match: "/v1/kt/claim",
      status: 403,
      body: envelope("permission_denied", sentence),
    });
    renderWithQuery(<KnowledgeTransferEntryPage />);

    await userEvent.type(screen.getByLabelText(/kt id/i), "  KT-JUTSU-AAAA0001  ");
    await userEvent.click(screen.getByRole("button", { name: /open kt/i }));

    expect(await screen.findByText(sentence)).toBeInTheDocument();
    expect(sentBody(fetchMock, callIndexFor(fetchMock, "/v1/kt/claim"))).toEqual({
      kt_code: "KT-JUTSU-AAAA0001",
    });
    expect(push).not.toHaveBeenCalled();
  });
});

describe("the KT console shell", () => {
  it("renders the workspace with subject, KT ID and every section tab", async () => {
    scriptFetch({ status: 200, body: recipientPackage() });
    renderWithQuery(
      <KtShell code="KT-JUTSU-AAAA0001">
        <p>Workspace body</p>
      </KtShell>,
    );

    expect(await screen.findByText("Grace Hopper")).toBeInTheDocument();
    expect(screen.getByText("KT-JUTSU-AAAA0001")).toBeInTheDocument();
    for (const tab of [
      "Overview",
      "Learning path",
      "Ask KT",
      "Documents",
      "Projects",
      "Responsibilities",
      "People",
      "Decisions",
      "Meetings",
      "Timeline",
      "Saved",
      "Handover",
    ]) {
      expect(screen.getByRole("link", { name: tab })).toBeInTheDocument();
    }
    expect(screen.getByText("Workspace body")).toBeInTheDocument();
  });

  it("renders a revoked package with the exact server sentence and no workspace", async () => {
    scriptFetch({
      status: 403,
      body: envelope("permission_denied", "This Knowledge Transfer package has been revoked."),
    });
    renderWithQuery(
      <KtShell code="KT-JUTSU-AAAA0001">
        <p>Workspace body</p>
      </KtShell>,
    );

    expect(
      await screen.findByText("This Knowledge Transfer package has been revoked."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Workspace body")).not.toBeInTheDocument();
    // Never the role-permission sentence: a revoked package is not a rank problem.
    expect(screen.queryByText(/your role does not include/i)).not.toBeInTheDocument();
  });

  it("closes the workspace when a package is revoked while it is open", async () => {
    // The regression this ordering exists for. TanStack keeps the last successful
    // `data` through a failed refetch, so a shell that tested `claim.data` first went
    // on rendering names, decisions and citations derived from evidence the recipient
    // may no longer read — the exact thing §39 forbids. Refetching against a 403 puts
    // the query into the one state that tells the two orderings apart: data AND error.
    scriptFetch(
      { status: 200, body: recipientPackage() },
      {
        status: 403,
        body: envelope("permission_denied", "This Knowledge Transfer package has been revoked."),
      },
    );
    const { client } = renderWithQuery(
      <KtShell code="KT-JUTSU-AAAA0001">
        <p>Workspace body</p>
      </KtShell>,
    );
    expect(await screen.findByText("Workspace body")).toBeInTheDocument();

    await act(async () => {
      await client.refetchQueries({ queryKey: ["kt", "open", "KT-JUTSU-AAAA0001"] });
    });

    expect(
      await screen.findByText("This Knowledge Transfer package has been revoked."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Workspace body")).not.toBeInTheDocument();
  });

  it("renders an expired package with the server's sentence", async () => {
    scriptFetch({
      status: 403,
      body: envelope("permission_denied", "This Knowledge Transfer package has expired."),
    });
    renderWithQuery(
      <KtShell code="KT-JUTSU-AAAA0001">
        <p>Workspace body</p>
      </KtShell>,
    );

    expect(
      await screen.findByText("This Knowledge Transfer package has expired."),
    ).toBeInTheDocument();
  });

  it("re-authorizes on every mount rather than trusting a cache", async () => {
    // First mount: fine. Second mount of the same code: the server now says revoked,
    // and revoked is what must render — §39 forbids a cache outliving revocation.
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      {
        status: 403,
        body: envelope("permission_denied", "This Knowledge Transfer package has been revoked."),
      },
    );

    const first = renderWithQuery(
      <KtShell code="KT-JUTSU-AAAA0001">
        <p>Workspace body</p>
      </KtShell>,
    );
    expect(await screen.findByText("Workspace body")).toBeInTheDocument();
    first.unmount();

    renderWithQuery(
      <KtShell code="KT-JUTSU-AAAA0001">
        <p>Workspace body</p>
      </KtShell>,
    );
    expect(
      await screen.findByText("This Knowledge Transfer package has been revoked."),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("the KT documents tab", () => {
  function ktDocument(overrides: Json = {}): Json {
    return {
      id: "11111111-1111-4111-8111-111111111111",
      title: "Handover plan",
      source_system: "gmail",
      created_at: "2026-08-01T00:00:00Z",
      ...overrides,
    };
  }

  it("retires Load more after the final page instead of restarting the walk", async () => {
    // Call 0 is the shell's claim; 1 and 2 are the two document pages.
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [ktDocument()], next_cursor: "cursor-2" } },
      {
        status: 200,
        body: {
          items: [
            ktDocument({ id: "22222222-2222-4222-8222-222222222222", title: "Q3 retro notes" }),
          ],
          next_cursor: null,
        },
      },
    );
    renderWithQuery(
      <KtShell code="KT-JUTSU-AAAA0001">
        <KtDocuments />
      </KtShell>,
    );
    await screen.findByText("Handover plan");

    await userEvent.click(screen.getByRole("button", { name: /load more/i }));

    // The walk ends when a page comes back with no cursor. Before the exhausted flag,
    // the null cursor fell back to the HEAD page's cursor — the button came back and
    // clicking it re-appended page two as duplicates.
    expect(await screen.findByText("Q3 retro notes")).toBeInTheDocument();
    expect(screen.getByText("Handover plan")).toBeInTheDocument();
    expect(calledUrl(fetchMock, 2)).toContain("cursor=cursor-2");
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
  });

  it("opens a document into the reader, renders its passages in order, and comes back", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [ktDocument()], next_cursor: null } },
      {
        status: 200,
        body: {
          ...ktDocument(),
          chunks: [
            { ordinal: 0, text: "The first passage of the handover plan." },
            { ordinal: 1, text: "The second passage, with [EMAIL_A7] masked." },
          ],
          total_chunks: 2,
          next_ordinal: null,
        },
      },
    );
    renderWithQuery(
      <KtShell code="KT-JUTSU-AAAA0001">
        <KtDocuments />
      </KtShell>,
    );

    await userEvent.click(await screen.findByRole("button", { name: /handover plan/i }));

    expect(
      await screen.findByText("The first passage of the handover plan."),
    ).toBeInTheDocument();
    expect(screen.getByText(/the second passage/i)).toBeInTheDocument();
    expect(calledUrl(fetchMock, 2)).toBe(
      "/api/jutsu/v1/kt/KT-JUTSU-AAAA0001/documents/11111111-1111-4111-8111-111111111111",
    );
    expect(calledMethod(fetchMock, 2)).toBe("GET");

    // The way out is part of the surface, not the browser's back button.
    await userEvent.click(screen.getByRole("button", { name: /back to documents/i }));
    expect(await screen.findByRole("button", { name: /handover plan/i })).toBeInTheDocument();
  });

  it("renders the honest not-available state when the document 404s", async () => {
    // A document the caller may no longer read, one outside the window and one that never
    // existed are the same 404 by design — so the reader may not call any of them a fault.
    scriptFetch(
      { status: 200, body: recipientPackage() },
      { status: 200, body: { items: [ktDocument()], next_cursor: null } },
      {
        status: 404,
        body: envelope("not_found", "That document is not available in this package."),
      },
    );
    renderWithQuery(
      <KtShell code="KT-JUTSU-AAAA0001">
        <KtDocuments />
      </KtShell>,
    );

    await userEvent.click(await screen.findByRole("button", { name: /handover plan/i }));

    expect(
      await screen.findByText(/this document is no longer available to you/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/that document is not available in this package/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/that did not load/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /back to documents/i })).toBeInTheDocument();
  });
});
