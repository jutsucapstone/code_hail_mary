import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import KnowledgeTransferEntryPage from "@/app/(product)/handover/page";
import { KtShell } from "@/components/kt/kt-shell";
import {
  calledMethod,
  calledUrl,
  envelope,
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
  it("claims through the API and navigates into the workspace", async () => {
    const fetchMock = scriptFetch({ status: 200, body: recipientPackage() });
    renderWithQuery(<KnowledgeTransferEntryPage />);

    await userEvent.type(screen.getByLabelText(/kt id/i), "KT-JUTSU-AAAA0001");
    await userEvent.click(screen.getByRole("button", { name: /open kt/i }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/kt/KT-JUTSU-AAAA0001"));
    expect(calledUrl(fetchMock, 0)).toBe("/api/jutsu/v1/kt/claim");
    expect(calledMethod(fetchMock, 0)).toBe("POST");
    expect(sentBody(fetchMock, 0)).toEqual({ kt_code: "KT-JUTSU-AAAA0001" });
  });

  it("renders the server's refusal for an unknown ID", async () => {
    scriptFetch({
      status: 404,
      body: envelope("not_found", "No package matches that ID. Check it with your administrator."),
    });
    renderWithQuery(<KnowledgeTransferEntryPage />);

    await userEvent.type(screen.getByLabelText(/kt id/i), "KT-JUTSU-WRONG000");
    await userEvent.click(screen.getByRole("button", { name: /open kt/i }));

    expect(await screen.findByText(/no package matches that id/i)).toBeInTheDocument();
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
      "Documents",
      "Ask KT",
      "Projects",
      "Responsibilities",
      "People",
      "Decisions",
      "Meetings",
      "Timeline",
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
