import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { SourceIdentities } from "@/components/admin/source-identities";
import type { Capabilities } from "@/lib/permissions";
import {
  calledMethod,
  calledUrl,
  capabilities as makeCapabilities,
  envelope,
  type Json,
  scriptFetch,
  sentBody,
} from "@/test-support/api";

/**
 * The screen that grants and removes document access.
 *
 * These assertions are mostly about *language* and *requests*, not layout, because the
 * hazard this component was written against is a naming one: a reader who believes they
 * are disconnecting a data feed when they are actually revoking a colleague's access to
 * documents.
 */

const EMPLOYEE_ID = "66666666-6666-4666-8666-666666666666";
const ADMIN_ID = "44444444-4444-4444-8444-444444444444";
const IDENTITY_ID = "77777777-7777-4777-8777-777777777777";

const EMPLOYEE = {
  id: EMPLOYEE_ID,
  email: "kean@example.com",
  display_name: "Steven Kean",
  role: "member" as const,
  status: "active",
  jutsu_id: "JUTSU-MEM-1234ABCD",
  created_at: "2026-01-01T00:00:00Z",
  last_activity_at: null,
};

function identity(overrides: Json = {}): Json {
  return {
    id: IDENTITY_ID,
    source_system: "local",
    subject: "steven.kean@enron.com",
    is_active: true,
    linked_at: "2026-08-30T14:05:44Z",
    revoked_at: null,
    linked_by: "admin",
    ...overrides,
  };
}

function caps(overrides: Json = {}): Capabilities {
  return makeCapabilities(overrides) as unknown as Capabilities;
}

function renderIt(capabilities = caps()) {
  return render(<SourceIdentities employee={EMPLOYEE} capabilities={capabilities} />);
}

beforeEach(() => {
  document.cookie = "";
});

describe("listing", () => {
  it("shows the assembled principal, because that is what a grant matches", async () => {
    scriptFetch({ status: 200, body: { items: [identity()] } });
    renderIt();

    // Not "local" and "steven.kean@enron.com" in separate columns — the string that
    // `document_acl.principal_id` actually holds.
    expect(await screen.findByText("local:steven.kean@enron.com")).toBeInTheDocument();
  });

  it("requests the employee's identities", async () => {
    const fetchMock = scriptFetch({ status: 200, body: { items: [identity()] } });
    renderIt();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(calledUrl(fetchMock)).toBe(`/api/jutsu/v1/employees/${EMPLOYEE_ID}/identities`);
    expect(calledMethod(fetchMock)).toBe("GET");
  });

  it("says plainly that an empty list means searches return nothing", async () => {
    scriptFetch({ status: 200, body: { items: [] } });
    renderIt();

    expect(await screen.findByText(/no linked identities/i)).toBeInTheDocument();
    expect(screen.getByText(/searches will correctly return nothing/i)).toBeInTheDocument();
  });

  it("distinguishes an active identity from a revoked one by word, not colour", async () => {
    scriptFetch({ status: 200, body: { items: [identity({ is_active: false })] } });
    renderIt();

    expect(await screen.findByText("Revoked")).toBeInTheDocument();
  });

  it("surfaces a load failure with its request id", async () => {
    scriptFetch({ status: 500, body: envelope("internal_error", "Something broke.") });
    renderIt();

    expect(await screen.findByRole("alert")).toHaveTextContent(/something broke/i);
    expect(screen.getByText(/req-abc/i)).toBeInTheDocument();
  });
});

describe("the copy", () => {
  it("says linking grants access, and never calls this an integration", async () => {
    scriptFetch({ status: 200, body: { items: [identity()] } });
    renderIt();

    await screen.findByText("local:steven.kean@enron.com");
    // Matches both the sentence and the emphasised span inside it — either is fine,
    // the claim is that the word appears at all.
    expect(screen.getAllByText(/grants\s+access/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/not an application connection/i)).toBeInTheDocument();
    // The word that would make a reader think this unplugs a data feed.
    expect(screen.queryByText(/disconnect/i)).not.toBeInTheDocument();
  });
});

describe("linking", () => {
  it("posts the source system and subject, and nothing else", async () => {
    const user = userEvent.setup();
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [] } },
      { status: 201, body: identity() },
      { status: 200, body: { items: [identity()] } },
    );
    renderIt();

    await screen.findByText(/no linked identities/i);
    await user.type(screen.getByLabelText("Subject"), "steven.kean@enron.com");
    await user.click(screen.getByRole("button", { name: /link identity/i }));

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
    expect(calledMethod(fetchMock, 1)).toBe("POST");
    expect(Object.keys(sentBody(fetchMock, 1)).sort()).toEqual(["source_system", "subject"]);
    expect(sentBody(fetchMock, 1).subject).toBe("steven.kean@enron.com");
  });

  it("shows the API's refusal rather than swallowing it", async () => {
    const user = userEvent.setup();
    scriptFetch(
      { status: 200, body: { items: [] } },
      { status: 403, body: envelope("permission_denied", "You cannot link a source identity to your own account.") },
    );
    renderIt();

    await screen.findByText(/no linked identities/i);
    await user.type(screen.getByLabelText("Subject"), "someone@example.com");
    await user.click(screen.getByRole("button", { name: /link identity/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/your own account/i);
  });

  it("explains the self-link rule instead of hiding the form", async () => {
    // The admin is looking at their own row. The API refuses this for an Owner too, so
    // the reader should learn the rule rather than find a missing control.
    scriptFetch({ status: 200, body: { items: [identity()] } });
    render(
      <SourceIdentities
        employee={{ ...EMPLOYEE, id: ADMIN_ID }}
        capabilities={caps()}
      />,
    );

    expect(await screen.findByText(/cannot link an identity to your own account/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Subject")).not.toBeInTheDocument();
  });
});

describe("revoking", () => {
  it("calls DELETE on the identity", async () => {
    const user = userEvent.setup();
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [identity()] } },
      { status: 204, body: null },
      { status: 200, body: { items: [identity({ is_active: false })] } },
    );
    renderIt();

    await user.click(await screen.findByRole("button", { name: /revoke access/i }));

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
    expect(calledUrl(fetchMock, 1)).toBe(
      `/api/jutsu/v1/employees/${EMPLOYEE_ID}/identities/${IDENTITY_ID}`,
    );
    expect(calledMethod(fetchMock, 1)).toBe("DELETE");
  });

  it("calls the control 'Revoke access', not 'Disconnect'", async () => {
    scriptFetch({ status: 200, body: { items: [identity()] } });
    renderIt();

    expect(await screen.findByRole("button", { name: /revoke access/i })).toBeInTheDocument();
  });
});

describe("permissions gate rendering only", () => {
  it("says so when the caller cannot read identities", () => {
    renderIt(caps({ permissions: ["profile:self_read"] }));

    expect(screen.getByText(/do not have access/i)).toBeInTheDocument();
  });

  it("offers no link form without integration:connect", async () => {
    scriptFetch({ status: 200, body: { items: [identity()] } });
    renderIt(caps({ permissions: ["integration:read"] }));

    await screen.findByText("local:steven.kean@enron.com");
    expect(screen.queryByRole("button", { name: /link identity/i })).not.toBeInTheDocument();
  });

  it("offers no revoke control without integration:revoke", async () => {
    scriptFetch({ status: 200, body: { items: [identity()] } });
    renderIt(caps({ permissions: ["integration:read"] }));

    await screen.findByText("local:steven.kean@enron.com");
    expect(screen.queryByRole("button", { name: /revoke access/i })).not.toBeInTheDocument();
  });
});
