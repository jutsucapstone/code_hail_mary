import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import KnowledgeTransferPage from "@/app/admin/knowledge-transfer/page";
import {
  calledMethod,
  calledUrl,
  callIndexFor,
  capabilities,
  scriptFetch,
  type ScriptedResponse,
  sentBody,
  type Json,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The admin side of knowledge transfer, against a scripted API.
 *
 * What these prove is the contract: which URL each action calls, that completion takes
 * two clicks, and that the activity panel renders the trail's own columns — an action,
 * an outcome, a timestamp and a JUTSU ID — and never asks for the trail without the
 * permission to read it. Whether the API enforces any of this is proven server-side.
 */

const caps: { current: Json } = { current: capabilities() };

vi.mock("@/components/admin/admin-shell", () => ({
  useCapabilities: () => caps.current,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/admin/knowledge-transfer",
}));

const DEFAULT_PERMISSIONS = capabilities().permissions as string[];

beforeEach(() => {
  caps.current = capabilities({
    permissions: [...DEFAULT_PERMISSIONS, "kt:manage", "audit:read"],
  });
});

const PACKAGE_ID = "12121212-1212-4121-8121-121212121212";

function ktAdmin(overrides: Json = {}): Json {
  return {
    id: PACKAGE_ID,
    kt_code: "KT-JUTSU-AAAA0001",
    subject_user_id: "99999999-9999-4999-8999-999999999999",
    subject_email: "grace@example.com",
    subject_name: "Grace Hopper",
    scope: ["documents", "profile"],
    period_start: null,
    period_end: null,
    status: "active",
    recipient_email: null,
    claimed_at: null,
    created_at: "2026-08-01T09:00:00Z",
    expires_at: "2026-10-01T09:00:00Z",
    last_activity_at: "2026-08-20T14:30:00Z",
    ...overrides,
  };
}

function auditEntry(overrides: Json = {}): Json {
  return {
    id: 41,
    actor_id: "44444444-4444-4444-8444-444444444444",
    actor_jutsu_id: "JUTSU-ADM-9HXPNFG8",
    actor_type: "user",
    action: "kt.opened",
    resource_type: "kt_package",
    resource_id: PACKAGE_ID,
    outcome: "success",
    ts: "2026-08-20T14:30:00Z",
    correlation_id: null,
    ...overrides,
  };
}

function listPage(...items: Json[]): Json {
  return { items, next_cursor: null };
}

/** The employee `ktAdmin()` is about, and a colleague who could take over from her. */
const SUBJECT_ID = "99999999-9999-4999-8999-999999999999";
const NEW_HIRE_ID = "88888888-8888-4888-8888-888888888888";

function employee(overrides: Json = {}): Json {
  return {
    id: NEW_HIRE_ID,
    email: "new.hire@example.com",
    display_name: "New Hire",
    jutsu_id: null,
    role: "member",
    status: "active",
    mapping_status: "unmapped",
    created_at: "2026-08-01T09:00:00Z",
    last_activity_at: null,
    ...overrides,
  };
}

/** This organisation's directory, as `GET /v1/employees` answers the pickers. */
const DIRECTORY: Json[] = [
  employee({ id: SUBJECT_ID, display_name: "Grace Hopper", email: "grace@example.com" }),
  employee(),
  employee({
    id: "77777777-7777-4777-8777-777777777777",
    display_name: "Left Already",
    email: "left@example.com",
    status: "deactivated",
  }),
];

/**
 * The positional script, with the package-attachment reads answered out of band.
 *
 * `KtAttachments` fires two GETs the moment the details panel opens, and neither is what
 * any test in this file is about. Letting them consume scripted responses would shift
 * every body by two and make each assertion depend on React's effect order — the exact
 * fragility `routeFetch` exists to avoid. What the panel itself does is proven in
 * `components/admin/kt-attachments.test.tsx`, against its own scripted API.
 *
 * The directory the recipient pickers read is answered the same way, for the same
 * reason: it is data for a picker, never the request a test is about.
 */
function script(...responses: ScriptedResponse[]) {
  const positional = scriptFetch(...responses);
  const fetchMock = vi.fn((input: unknown, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/attachments") || url.includes("/attachable")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ items: [] }) });
    }
    if (url.includes("/v1/employees")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ items: DIRECTORY, next_cursor: null }),
      });
    }
    return positional(input, init) as Promise<unknown>;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The one PATCH in a script. The detail GET shares its URL, so the method is the key. */
function patchIndex(fetchMock: ReturnType<typeof scriptFetch>): number {
  return fetchMock.mock.calls.findIndex(
    (call) => (call[1] as RequestInit | undefined)?.method === "PATCH",
  );
}

describe("knowledge transfer list", () => {
  it("renders each package with its last activity", async () => {
    script({
      status: 200,
      body: listPage(
        ktAdmin(),
        ktAdmin({
          id: "34343434-3434-4343-8343-343434343434",
          kt_code: "KT-JUTSU-BBBB0002",
          subject_name: "Ada Lovelace",
          last_activity_at: null,
        }),
      ),
    });
    renderWithQuery(<KnowledgeTransferPage />);

    expect(await screen.findByText("Grace Hopper")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Last activity" })).toBeInTheDocument();

    const grace = screen.getByRole("row", { name: /grace hopper/i });
    const graceCells = within(grace).getAllByRole("cell");
    // Columns: KT ID, Status, Recipient, Created, Expires, Last activity, Actions.
    const lastActivity = graceCells[5];
    expect(within(lastActivity).getByText((_, el) => el?.tagName === "TIME")).toHaveAttribute(
      "dateTime",
      "2026-08-20T14:30:00Z",
    );

    // A package nobody has opened has no activity, and says so with a dash — never a
    // made-up date and never an empty cell.
    const ada = screen.getByRole("row", { name: /ada lovelace/i });
    expect(within(ada).getAllByRole("cell")[5]).toHaveTextContent("—");
    expect(within(ada).getByText("Bound to first opener")).toBeInTheDocument();
  });

  it("completes a package only on the second click, then POSTs to its complete route", async () => {
    const fetchMock = script(
      { status: 200, body: listPage(ktAdmin()) },
      { status: 200, body: ktAdmin({ status: "completed" }) },
      { status: 200, body: listPage(ktAdmin({ status: "completed" })) },
    );
    renderWithQuery(<KnowledgeTransferPage />);
    await screen.findByText("Grace Hopper");

    await userEvent.click(screen.getByRole("button", { name: "Complete KT-JUTSU-AAAA0001" }));

    // The first click asks; nothing has been sent yet.
    const confirm = screen.getByRole("button", { name: "Confirm completing KT-JUTSU-AAAA0001" });
    expect(confirm).toHaveTextContent("Confirm complete?");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await userEvent.click(confirm);

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2));
    const post = callIndexFor(fetchMock, "/complete");
    expect(calledUrl(fetchMock, post)).toBe(`/api/jutsu/v1/kt/${PACKAGE_ID}/complete`);
    expect(calledMethod(fetchMock, post)).toBe("POST");

    // The list refetches rather than being patched locally; the completed row loses
    // its lifecycle actions because the server now says it is terminal.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /complete kt-jutsu-aaaa0001/i })).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Revoke KT-JUTSU-AAAA0001" })).not.toBeInTheDocument();
  });

  it("offers Complete only on packages that are still open", async () => {
    script({ status: 200, body: listPage(ktAdmin({ status: "revoked" })) });
    renderWithQuery(<KnowledgeTransferPage />);
    await screen.findByText("Grace Hopper");

    expect(screen.queryByRole("button", { name: /^complete/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^revoke/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Details for KT-JUTSU-AAAA0001" })).toBeInTheDocument();
  });
});

describe("knowledge transfer details", () => {
  it("fetches the package and its slice of the audit trail, and renders an activity row", async () => {
    const fetchMock = script(
      { status: 200, body: listPage(ktAdmin()) },
      { status: 200, body: ktAdmin({ claimed_at: "2026-08-02T10:00:00Z", status: "claimed" }) },
      {
        status: 200,
        body: {
          items: [
            auditEntry(),
            auditEntry({
              id: 40,
              action: "kt.open",
              outcome: "denied",
              actor_jutsu_id: null,
              ts: "2026-08-19T08:00:00Z",
            }),
          ],
          next_cursor: null,
        },
      },
    );
    renderWithQuery(<KnowledgeTransferPage />);
    await screen.findByText("Grace Hopper");

    await userEvent.click(screen.getByRole("button", { name: "Details for KT-JUTSU-AAAA0001" }));

    const panel = await screen.findByRole("region", { name: "Package details" });
    expect(await within(panel).findByText("kt.opened")).toBeInTheDocument();

    const detailCall = callIndexFor(fetchMock, `/v1/kt/${PACKAGE_ID}`);
    expect(calledUrl(fetchMock, detailCall)).toBe(`/api/jutsu/v1/kt/${PACKAGE_ID}`);
    expect(calledMethod(fetchMock, detailCall)).toBe("GET");

    const auditCall = callIndexFor(fetchMock, "/v1/audit?");
    expect(calledUrl(fetchMock, auditCall)).toBe(
      `/api/jutsu/v1/audit?resource_type=kt_package&resource_id=${PACKAGE_ID}&limit=20`,
    );
    expect(calledMethod(fetchMock, auditCall)).toBe("GET");

    // The package record, as GET /v1/kt/{id} returned it.
    expect(within(panel).getByText("Claimed")).toBeInTheDocument();
    expect(within(panel).getByText("Full history")).toBeInTheDocument();
    expect(within(panel).getByText("Role & profile")).toBeInTheDocument();

    // The trail's own columns: action, outcome, actor. A refused open has no JUTSU ID
    // resolved for it, so the actor type stands in — never an address.
    const rows = within(panel).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("success");
    expect(rows[0]).toHaveTextContent("JUTSU-ADM-9HXPNFG8");
    expect(rows[1]).toHaveTextContent("kt.open");
    expect(rows[1]).toHaveTextContent("denied");
    expect(rows[1]).toHaveTextContent("user");
    expect(panel).not.toHaveTextContent("@");

    expect(within(panel).getByRole("link", { name: "Open the full audit trail" })).toHaveAttribute(
      "href",
      "/admin/audit",
    );
  });

  it("says when a package has no activity yet", async () => {
    script(
      { status: 200, body: listPage(ktAdmin({ last_activity_at: null })) },
      { status: 200, body: ktAdmin({ last_activity_at: null }) },
      { status: 200, body: { items: [], next_cursor: null } },
    );
    renderWithQuery(<KnowledgeTransferPage />);
    await screen.findByText("Grace Hopper");

    await userEvent.click(screen.getByRole("button", { name: "Details for KT-JUTSU-AAAA0001" }));

    expect(
      await screen.findByText("No activity recorded for this package yet."),
    ).toBeInTheDocument();
  });

  it("does not ask for the trail without audit:read, and says why", async () => {
    caps.current = capabilities({ permissions: [...DEFAULT_PERMISSIONS, "kt:manage"] });
    const fetchMock = script(
      { status: 200, body: listPage(ktAdmin()) },
      { status: 200, body: ktAdmin() },
    );
    renderWithQuery(<KnowledgeTransferPage />);
    await screen.findByText("Grace Hopper");

    await userEvent.click(screen.getByRole("button", { name: "Details for KT-JUTSU-AAAA0001" }));

    const panel = await screen.findByRole("region", { name: "Package details" });
    expect(await within(panel).findByText("KT-JUTSU-AAAA0001")).toBeInTheDocument();
    expect(within(panel).getByText("Your role cannot read the audit trail.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.map((call) => String(call[0]))).not.toContainEqual(
      expect.stringContaining("/v1/audit"),
    );
  });

  it("extends the expiry with a PATCH carrying only extend_days", async () => {
    const extended = ktAdmin({ expires_at: "2026-11-30T09:00:00Z" });
    const fetchMock = script(
      { status: 200, body: listPage(ktAdmin()) },
      { status: 200, body: ktAdmin() },
      { status: 200, body: { items: [], next_cursor: null } },
      { status: 200, body: extended },
      // The record, its trail and the list re-read after the change.
      { status: 200, body: extended },
      { status: 200, body: { items: [], next_cursor: null } },
      { status: 200, body: listPage(extended) },
    );
    renderWithQuery(<KnowledgeTransferPage />);
    await screen.findByText("Grace Hopper");
    await userEvent.click(screen.getByRole("button", { name: "Details for KT-JUTSU-AAAA0001" }));
    const panel = await screen.findByRole("region", { name: "Package details" });

    await userEvent.selectOptions(within(panel).getByLabelText("Extend expiry by"), "60");
    await userEvent.click(within(panel).getByRole("button", { name: "Extend expiry" }));

    await waitFor(() => expect(patchIndex(fetchMock)).toBeGreaterThan(-1));
    const patch = patchIndex(fetchMock);
    expect(calledUrl(fetchMock, patch)).toBe(`/api/jutsu/v1/kt/${PACKAGE_ID}`);
    expect(sentBody(fetchMock, patch)).toEqual({ extend_days: 60 });

    // The list re-reads rather than being patched locally: a second GET of the head page.
    await waitFor(() =>
      expect(fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/v1/kt"))).toHaveLength(2),
    );
  });

  it("re-addresses a package nobody has opened to a colleague picked from the directory", async () => {
    const readdressed = ktAdmin({ recipient_email: "new.hire@example.com" });
    const fetchMock = script(
      { status: 200, body: listPage(ktAdmin()) },
      { status: 200, body: ktAdmin() },
      { status: 200, body: { items: [], next_cursor: null } },
      { status: 200, body: readdressed },
      { status: 200, body: readdressed },
      { status: 200, body: { items: [], next_cursor: null } },
      { status: 200, body: listPage(readdressed) },
    );
    renderWithQuery(<KnowledgeTransferPage />);
    await screen.findByText("Grace Hopper");
    await userEvent.click(screen.getByRole("button", { name: "Details for KT-JUTSU-AAAA0001" }));
    const panel = await screen.findByRole("region", { name: "Package details" });

    const recipients = await within(panel).findByRole("list", { name: "Recipients" });
    // Never the employee the package is about, and never an account that cannot sign in.
    expect(within(recipients).queryByRole("button", { name: /grace hopper/i })).not.toBeInTheDocument();
    expect(within(recipients).queryByRole("button", { name: /left already/i })).not.toBeInTheDocument();
    await userEvent.click(within(recipients).getByRole("button", { name: /new hire/i }));
    await userEvent.click(within(panel).getByRole("button", { name: "Re-address" }));

    await waitFor(() => expect(patchIndex(fetchMock)).toBeGreaterThan(-1));
    expect(sentBody(fetchMock, patchIndex(fetchMock))).toEqual({
      recipient_email: "new.hire@example.com",
    });
    // The record re-reads and now names its recipient.
    await waitFor(() =>
      expect(within(panel).getByText("Recipient").parentElement).toHaveTextContent(
        "new.hire@example.com",
      ),
    );
  });

  it("offers no re-address once the package is bound, and neither control once it is terminal", async () => {
    const bound = ktAdmin({ status: "claimed", claimed_at: "2026-08-02T10:00:00Z" });
    const revoked = ktAdmin({
      id: "34343434-3434-4343-8343-343434343434",
      kt_code: "KT-JUTSU-BBBB0002",
      subject_name: "Ada Lovelace",
      status: "revoked",
    });
    script(
      { status: 200, body: listPage(bound, revoked) },
      { status: 200, body: bound },
      { status: 200, body: { items: [], next_cursor: null } },
      { status: 200, body: revoked },
      { status: 200, body: { items: [], next_cursor: null } },
    );
    renderWithQuery(<KnowledgeTransferPage />);
    await screen.findByText("Grace Hopper");

    await userEvent.click(screen.getByRole("button", { name: "Details for KT-JUTSU-AAAA0001" }));
    let panel = await screen.findByRole("region", { name: "Package details" });
    expect(await within(panel).findByRole("button", { name: "Extend expiry" })).toBeInTheDocument();
    expect(within(panel).queryByRole("group", { name: "Re-address to" })).not.toBeInTheDocument();
    expect(within(panel).getByText(/can no longer be re-addressed/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Details for KT-JUTSU-BBBB0002" }));
    panel = await screen.findByRole("region", { name: "Package details" });
    expect(await within(panel).findByText("KT-JUTSU-BBBB0002")).toBeInTheDocument();
    expect(within(panel).queryByRole("heading", { name: "Manage" })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Extend expiry" })).not.toBeInTheDocument();
  });

  it("closes the panel and returns to the list", async () => {
    script(
      { status: 200, body: listPage(ktAdmin()) },
      { status: 200, body: ktAdmin() },
      { status: 200, body: { items: [], next_cursor: null } },
    );
    renderWithQuery(<KnowledgeTransferPage />);
    await screen.findByText("Grace Hopper");

    await userEvent.click(screen.getByRole("button", { name: "Details for KT-JUTSU-AAAA0001" }));
    const panel = await screen.findByRole("region", { name: "Package details" });
    expect(within(panel).getByRole("heading", { name: "Package details" })).toHaveFocus();

    await userEvent.click(within(panel).getByRole("button", { name: "Close details" }));

    expect(screen.queryByRole("region", { name: "Package details" })).not.toBeInTheDocument();
  });
});

describe("creating a package", () => {
  /** The index of the one POST in a script: the create. */
  function postIndex(fetchMock: ReturnType<typeof scriptFetch>): number {
    return fetchMock.mock.calls.findIndex(
      (call) => (call[1] as RequestInit | undefined)?.method === "POST",
    );
  }

  it("hands the package to a colleague picked from the directory, never to its own employee", async () => {
    const fetchMock = script(
      { status: 200, body: listPage() },
      { status: 200, body: { supported: ["documents", "profile"] } },
      { status: 201, body: ktAdmin({ recipient_email: "new.hire@example.com" }) },
    );
    renderWithQuery(<KnowledgeTransferPage />);

    await userEvent.click(await screen.findByRole("button", { name: "+ Create KT" }));
    const employees = await screen.findByRole("list", { name: "Employees" });
    await userEvent.click(within(employees).getByRole("button", { name: /grace hopper/i }));

    const recipients = screen.getByRole("list", { name: "Recipients" });
    // The employee the package is about is not someone it can be handed to, and neither
    // is an account that cannot sign in.
    expect(within(recipients).queryByRole("button", { name: /grace hopper/i })).not.toBeInTheDocument();
    expect(within(recipients).queryByRole("button", { name: /left already/i })).not.toBeInTheDocument();
    await userEvent.click(within(recipients).getByRole("button", { name: /new hire/i }));
    expect(screen.getByText(/openable for 30 days by new hire\./i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Generate KT" }));

    await waitFor(() => expect(postIndex(fetchMock)).toBeGreaterThan(-1));
    const post = postIndex(fetchMock);
    expect(calledUrl(fetchMock, post)).toBe("/api/jutsu/v1/kt");
    expect(sentBody(fetchMock, post)).toMatchObject({
      subject_user_id: SUBJECT_ID,
      recipient_email: "new.hire@example.com",
    });
    expect(
      await screen.findByRole("heading", { name: "KT created successfully" }),
    ).toBeInTheDocument();
  });

  it("lets a package go unaddressed, to the first colleague who opens it", async () => {
    const fetchMock = script(
      { status: 200, body: listPage() },
      { status: 200, body: { supported: ["documents", "profile"] } },
      { status: 201, body: ktAdmin() },
    );
    renderWithQuery(<KnowledgeTransferPage />);

    await userEvent.click(await screen.findByRole("button", { name: "+ Create KT" }));
    const employees = await screen.findByRole("list", { name: "Employees" });
    await userEvent.click(within(employees).getByRole("button", { name: /grace hopper/i }));
    expect(screen.getByText(/by the first colleague who opens it\./i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Generate KT" }));

    await waitFor(() => expect(postIndex(fetchMock)).toBeGreaterThan(-1));
    expect(sentBody(fetchMock, postIndex(fetchMock))).toMatchObject({ recipient_email: null });
  });
});

describe("knowledge transfer gate", () => {
  it("denies without kt:manage instead of rendering an empty list", () => {
    caps.current = capabilities({ permissions: [...DEFAULT_PERMISSIONS, "audit:read"] });
    script();
    renderWithQuery(<KnowledgeTransferPage />);

    expect(screen.getByRole("heading", { name: /do not have access/i })).toBeInTheDocument();
  });
});

describe("revoking a package", () => {
  it("takes two clicks, because it is terminal and has no undo", async () => {
    // Revoke shipped on a single click beside a Complete that required two — backwards,
    // since completing is the intended end of a handover and revoking takes a
    // recipient's access away mid-flight with nothing to reverse it.
    const fetchMock = script(
      { status: 200, body: listPage(ktAdmin()) },
      { status: 200, body: ktAdmin({ status: "revoked" }) },
      { status: 200, body: listPage(ktAdmin({ status: "revoked" })) },
    );
    renderWithQuery(<KnowledgeTransferPage />);
    await screen.findByText("Grace Hopper");

    await userEvent.click(screen.getByRole("button", { name: "Revoke KT-JUTSU-AAAA0001" }));

    // The first click asks; nothing has been sent yet.
    const confirm = screen.getByRole("button", { name: "Confirm revoking KT-JUTSU-AAAA0001" });
    expect(confirm).toHaveTextContent("Confirm revoke?");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await userEvent.click(confirm);

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2));
    const post = callIndexFor(fetchMock, "/revoke");
    expect(calledUrl(fetchMock, post)).toBe(`/api/jutsu/v1/kt/${PACKAGE_ID}/revoke`);
    expect(calledMethod(fetchMock, post)).toBe("POST");
  });

  it("arms only one action at a time", async () => {
    // Arming Complete and then Revoke must leave exactly one question on screen — two
    // armed buttons is how a second click lands on the one the reader forgot about.
    script({ status: 200, body: listPage(ktAdmin()) });
    renderWithQuery(<KnowledgeTransferPage />);
    await screen.findByText("Grace Hopper");

    await userEvent.click(screen.getByRole("button", { name: "Complete KT-JUTSU-AAAA0001" }));
    await userEvent.click(screen.getByRole("button", { name: "Revoke KT-JUTSU-AAAA0001" }));

    expect(
      screen.getByRole("button", { name: "Confirm revoking KT-JUTSU-AAAA0001" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Confirm completing KT-JUTSU-AAAA0001" }),
    ).not.toBeInTheDocument();
  });
});
