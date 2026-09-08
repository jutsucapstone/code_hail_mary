import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import InvitationsPage from "@/app/admin/invitations/page";
import {
  calledMethod,
  calledUrl,
  capabilities,
  envelope,
  routeFetch,
  scriptFetch,
  type Json,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The Invitations page, and the two things an administrator can now do from it.
 *
 * What these prove is the contract — which URL each button calls, and that neither is
 * offered on a row the server would refuse. Whether the API actually revokes the token or
 * re-checks the rank ceiling is proven by `test_invitation_lifecycle.py` against real
 * Postgres; a scripted `fetch` would agree with whatever this component asked it.
 */

const caps: { current: Json } = { current: capabilities() };

vi.mock("@/components/admin/admin-shell", () => ({
  useCapabilities: () => caps.current,
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

beforeEach(() => {
  caps.current = capabilities({
    permissions: ["member:read", "member:invite", "profile:self_read", "retrieval:query"],
  });
});

function invitation(overrides: Json = {}): Json {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    email: "waiting@example.com",
    role: "member",
    status: "pending",
    created_at: "2026-09-01T10:00:00Z",
    expires_at: "2026-09-04T10:00:00Z",
    accepted_at: null,
    revoked_at: null,
    ...overrides,
  };
}

function page(...items: Json[]): Json {
  return { items, next_cursor: null };
}

describe("what each row offers", () => {
  it("offers Resend and Cancel on an invitation that is still waiting", async () => {
    scriptFetch({ status: 200, body: page(invitation()) });
    renderWithQuery(<InvitationsPage />);

    const row = (await screen.findByText("waiting@example.com")).closest("tr")!;
    expect(
      within(row).getByRole("button", { name: "Resend the invitation to waiting@example.com" }),
    ).toBeInTheDocument();
    expect(
      within(row).getByRole("button", { name: "Cancel the invitation to waiting@example.com" }),
    ).toBeInTheDocument();
  });

  it("offers neither on an accepted invitation", async () => {
    // That person is a member now. Removing a member is deactivation, under a different
    // permission — not this button, and the server answers 404 here anyway.
    scriptFetch({
      status: 200,
      body: page(invitation({ status: "accepted", accepted_at: "2026-09-02T10:00:00Z" })),
    });
    renderWithQuery(<InvitationsPage />);

    const row = (await screen.findByText("waiting@example.com")).closest("tr")!;
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
  });

  it("offers neither on an already-cancelled invitation", async () => {
    scriptFetch({
      status: 200,
      body: page(invitation({ status: "revoked", revoked_at: "2026-09-02T10:00:00Z" })),
    });
    renderWithQuery(<InvitationsPage />);

    const row = (await screen.findByText("waiting@example.com")).closest("tr")!;
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
  });

  it("still offers both on an EXPIRED invitation", async () => {
    // The one that is easy to get wrong. An expired invitation is exactly the row an
    // administrator wants to resend, and the API retires the stale row before issuing a
    // replacement — so hiding the button here would remove the feature's main use.
    scriptFetch({ status: 200, body: page(invitation({ status: "expired" })) });
    renderWithQuery(<InvitationsPage />);

    const row = (await screen.findByText("waiting@example.com")).closest("tr")!;
    expect(within(row).getAllByRole("button")).toHaveLength(2);
  });
});

describe("cancelling", () => {
  it("posts to the revoke route and refetches rather than patching the row", async () => {
    const fetchMock = routeFetch(
      { match: "/v1/invitations", status: 200, body: page(invitation()) },
      {
        match: "/revoke",
        status: 200,
        body: { email: "waiting@example.com" },
      },
      { match: "/v1/invitations", status: 200, body: page(invitation({ status: "revoked" })) },
    );
    renderWithQuery(<InvitationsPage />);

    await userEvent.click(
      await screen.findByRole("button", {
        name: "Cancel the invitation to waiting@example.com",
      }),
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(calledUrl(fetchMock, 1)).toContain(
      "/v1/invitations/11111111-1111-4111-8111-111111111111/revoke",
    );
    expect(calledMethod(fetchMock, 1)).toBe("POST");
    // The third call is the refetch. Status is derived from the database clock, so a
    // locally-edited row would be the one thing on this page that was not.
    expect(calledUrl(fetchMock, 2)).toContain("/v1/invitations");
  });

  it("surfaces the API's refusal and leaves the row alone", async () => {
    routeFetch(
      { match: "/v1/invitations", status: 200, body: page(invitation()) },
      {
        match: "/revoke",
        status: 404,
        body: envelope("not_found", "That invitation is no longer waiting."),
      },
    );
    const { toast } = await import("sonner");
    renderWithQuery(<InvitationsPage />);

    await userEvent.click(
      await screen.findByRole("button", {
        name: "Cancel the invitation to waiting@example.com",
      }),
    );

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("That invitation is no longer waiting."),
    );
    expect(screen.getByText("waiting@example.com")).toBeInTheDocument();
  });
});

describe("resending", () => {
  it("posts to the resend route", async () => {
    const fetchMock = routeFetch(
      { match: "/v1/invitations", status: 200, body: page(invitation()) },
      { match: "/resend", status: 202, body: { status: "sent" } },
      { match: "/v1/invitations", status: 200, body: page(invitation()) },
    );
    renderWithQuery(<InvitationsPage />);

    await userEvent.click(
      await screen.findByRole("button", {
        name: "Resend the invitation to waiting@example.com",
      }),
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(calledUrl(fetchMock, 1)).toContain(
      "/v1/invitations/11111111-1111-4111-8111-111111111111/resend",
    );
    expect(calledMethod(fetchMock, 1)).toBe("POST");
  });

  it("disables that row's own buttons while it is in flight, not the whole table", async () => {
    routeFetch(
      {
        match: "/v1/invitations",
        status: 200,
        body: page(
          invitation(),
          invitation({ id: "22222222-2222-4222-8222-222222222222", email: "other@example.com" }),
        ),
      },
      { match: "/resend", status: 202, body: { status: "sent" }, pending: true },
    );
    renderWithQuery(<InvitationsPage />);

    await userEvent.click(
      await screen.findByRole("button", {
        name: "Resend the invitation to waiting@example.com",
      }),
    );

    const other = (await screen.findByText("other@example.com")).closest("tr")!;
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Cancel the invitation to waiting@example.com" }),
      ).toBeDisabled(),
    );
    // A whole table that freezes on one slow request reads as a broken page.
    expect(
      within(other).getByRole("button", {
        name: "Resend the invitation to other@example.com",
      }),
    ).toBeEnabled();
  });
});

describe("permission", () => {
  it("shows a denial rather than an empty table without member:invite", async () => {
    caps.current = capabilities({ permissions: ["member:read", "profile:self_read"] });
    const fetchMock = scriptFetch({ status: 200, body: page(invitation()) });
    renderWithQuery(<InvitationsPage />);

    expect(await screen.findByText(/permission to see invitations/i)).toBeInTheDocument();
    // And it does not ask for data it may not have.
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
