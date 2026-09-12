import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AuditPage from "@/app/admin/audit/page";
import EmployeesPage from "@/app/admin/employees/page";
import HealthPage from "@/app/admin/health/page";
import SettingsPage from "@/app/admin/settings/page";
import { calledMethod, calledUrl, callIndexFor, capabilities, envelope, routeFetch, scriptFetch, sentBody, type Json, type RoutedResponse } from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The new admin surfaces, against a scripted API.
 *
 * What these prove is the contract: which URL each page calls, what a mutation sends,
 * and that a caller without the permission sees a denial rather than a blank panel.
 * Whether the API itself enforces anything is proven by `test_admin_operations.py`
 * against real Postgres — never here.
 */

const caps: { current: Json } = { current: capabilities() };

vi.mock("@/components/admin/admin-shell", () => ({
  useCapabilities: () => caps.current,
}));

beforeEach(() => {
  caps.current = capabilities({
    permissions: [
      "org:read",
      "org:update",
      "member:read",
      "member:invite",
      "member:assign_role",
      "integration:read",
      "audit:read",
      "profile:self_read",
      "retrieval:query",
    ],
  });
});

function auditEntry(overrides: Json = {}): Json {
  return {
    id: 7,
    actor_id: "44444444-4444-4444-8444-444444444444",
    actor_jutsu_id: "JUTSU-ADM-9HXPNFG8",
    actor_type: "user",
    action: "member.role_changed",
    resource_type: "user",
    resource_id: "abc",
    outcome: "success",
    ts: "2026-09-01T10:00:00Z",
    correlation_id: null,
    ...overrides,
  };
}

describe("audit page", () => {
  it("renders the trail with JUTSU IDs and outcomes", async () => {
    scriptFetch({
      status: 200,
      body: { items: [auditEntry()], next_cursor: null },
    });
    renderWithQuery(<AuditPage />);

    expect(await screen.findByText("member.role_changed")).toBeInTheDocument();
    expect(screen.getByText("JUTSU-ADM-9HXPNFG8")).toBeInTheDocument();
    // "success" is also a filter button; the row's pill is the one inside the table.
    const table = screen.getByRole("table");
    expect(table).toHaveTextContent("success");
  });

  it("asks the server to filter rather than filtering one page locally", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [auditEntry()], next_cursor: null } },
      { status: 200, body: { items: [], next_cursor: null } },
    );
    renderWithQuery(<AuditPage />);
    await screen.findByText("member.role_changed");

    await userEvent.click(screen.getByRole("button", { name: "denied" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(calledUrl(fetchMock, 1)).toContain("outcome=denied");
  });

  it("denies without audit:read instead of rendering an empty page", () => {
    caps.current = capabilities({ permissions: ["org:read", "profile:self_read"] });
    scriptFetch();
    renderWithQuery(<AuditPage />);

    expect(screen.getByRole("heading", { name: /do not have access/i })).toBeInTheDocument();
  });

  it("shows an empty trail as an explained state, not a blank table", async () => {
    scriptFetch({ status: 200, body: { items: [], next_cursor: null } });
    renderWithQuery(<AuditPage />);

    expect(await screen.findByText(/nothing recorded yet/i)).toBeInTheDocument();
  });

  it("retires Load more after the final page instead of restarting the walk", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [auditEntry()], next_cursor: "cursor-2" } },
      {
        status: 200,
        body: { items: [auditEntry({ id: 8, action: "invitation.sent" })], next_cursor: null },
      },
    );
    renderWithQuery(<AuditPage />);
    await screen.findByText("member.role_changed");

    await userEvent.click(screen.getByRole("button", { name: /load more/i }));

    // The walk ends when a page comes back with no cursor. Before the exhausted flag,
    // the null cursor fell back to the HEAD page's cursor — the button came back and
    // clicking it re-appended page two as duplicates.
    expect(await screen.findByText("invitation.sent")).toBeInTheDocument();
    expect(calledUrl(fetchMock, callIndexFor(fetchMock, "cursor=cursor-2"))).toContain(
      "cursor=cursor-2",
    );
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
  });
});

describe("settings page", () => {
  /**
   * Routed rather than positional: the page also reads the nightly sync schedule at
   * mount, and effects flush children before parents, so which of the two GETs reaches
   * `fetch` first is React's business. The schedule route is listed FIRST because
   * `/v1/orgs/current` is a prefix of `/v1/orgs/current/sync-schedule` and the broader
   * match would otherwise answer it. The schedule's own behaviour is covered in
   * `app/admin/settings/page.test.tsx`.
   */
  function scheduleRoute(): RoutedResponse {
    return {
      match: "/v1/orgs/current/sync-schedule",
      status: 200,
      body: {
        timezone: "Asia/Kolkata",
        hour_local: 1,
        enabled: true,
        next_sync_at: "2026-09-08T19:30:00Z",
        last_started_at: null,
        last_finished_at: null,
        last_outcome: null,
        last_connections: null,
        last_enqueued: null,
      },
    };
  }

  function orgProfile(): Json {
    return {
      id: "55555555-5555-4555-8555-555555555555",
      name: "Northwind",
      domain: "northwind.example",
      size_band: "51-200",
      status: "active",
      created_at: "2026-01-01T00:00:00Z",
      members: { total: 3, active: 3, invited: 0, deactivated: 0, admins: 1 },
    };
  }

  function orgRoute(body: Json = orgProfile()): RoutedResponse {
    return { match: "/v1/orgs/current", status: 200, body };
  }

  it("renames through the API and not through local state", async () => {
    const fetchMock = routeFetch(
      scheduleRoute(),
      orgRoute(),
      orgRoute({ name: "Northwind Industries" }),
      orgRoute({ ...orgProfile(), name: "Northwind Industries" }),
    );
    renderWithQuery(<SettingsPage />);

    const input = await screen.findByLabelText(/organisation name/i);
    await userEvent.clear(input);
    await userEvent.type(input, "Northwind Industries");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    // Both mount-time GETs are issued before anything is typed, so the mutation is the
    // third call whichever order they went out in.
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3));
    expect(calledUrl(fetchMock, 2)).toBe("/api/jutsu/v1/orgs/current");
    expect(calledMethod(fetchMock, 2)).toBe("PATCH");
    expect(sentBody(fetchMock, 2)).toEqual({ name: "Northwind Industries" });
  });

  it("shows the API's refusal instead of swallowing it", async () => {
    routeFetch(scheduleRoute(), orgRoute(), {
      match: "/v1/orgs/current",
      status: 422,
      body: envelope("validation_failed", "The organisation needs a name."),
    });
    renderWithQuery(<SettingsPage />);

    const input = await screen.findByLabelText(/organisation name/i);
    await userEvent.clear(input);
    await userEvent.type(input, "x");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText(/needs a name/i)).toBeInTheDocument();
  });

  it("denies without org:update", async () => {
    caps.current = capabilities({ permissions: ["org:read", "profile:self_read"] });
    routeFetch(scheduleRoute());
    renderWithQuery(<SettingsPage />);

    expect(screen.getByRole("heading", { name: /do not have access/i })).toBeInTheDocument();
    // The sync schedule below the denial is readable by every role; awaiting it also
    // settles the query inside the test rather than after it.
    expect(await screen.findByText(/^Runs at$/)).toBeInTheDocument();
  });

  it("renders the domain as fixed fact, never as a field", async () => {
    routeFetch(scheduleRoute(), orgRoute());
    renderWithQuery(<SettingsPage />);

    expect(await screen.findByText("northwind.example")).toBeInTheDocument();
    expect(screen.queryByLabelText(/domain/i)).not.toBeInTheDocument();
  });
});

describe("employees page role control", () => {
  function employee(overrides: Json = {}): Json {
    return {
      id: "99999999-9999-4999-8999-999999999999",
      email: "grace@example.com",
      display_name: "Grace Hopper",
      jutsu_id: "JUTSU-EMP-AAAAAAAA",
      status: "active",
      role: "member",
      created_at: "2026-02-01T00:00:00Z",
      last_activity_at: null,
      ...overrides,
    };
  }

  it("PATCHes the chosen role to the member's own endpoint", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [employee()], next_cursor: null } },
      {
        status: 200,
        body: {
          user_id: "99999999-9999-4999-8999-999999999999",
          role: "analyst",
          previous_role: "member",
        },
      },
      { status: 200, body: { items: [employee({ role: "analyst" })], next_cursor: null } },
    );
    renderWithQuery(<EmployeesPage />);

    const select = await screen.findByLabelText(/change role for grace hopper/i);
    await userEvent.selectOptions(select, "analyst");

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2));
    // Found by URL: the page also fetches the role catalogue for its filters, so the
    // PATCH is no longer at a fixed position in the call list.
    const patch = callIndexFor(fetchMock, "999999999999/role");
    expect(calledUrl(fetchMock, patch)).toBe(
      "/api/jutsu/v1/employees/99999999-9999-4999-8999-999999999999/role",
    );
    expect(calledMethod(fetchMock, patch)).toBe("PATCH");
    expect(sentBody(fetchMock, patch)).toEqual({ role: "analyst" });
  });

  it("offers no control on the caller's own row", async () => {
    caps.current = capabilities({
      user_id: "99999999-9999-4999-8999-999999999999",
      permissions: ["member:read", "member:assign_role", "profile:self_read"],
    });
    scriptFetch({ status: 200, body: { items: [employee()], next_cursor: null } });
    renderWithQuery(<EmployeesPage />);

    // The server refuses self-changes even for the owner; the row says so instead of
    // offering a dropdown that cannot work.
    expect(await screen.findByText("You")).toBeInTheDocument();
    expect(screen.queryByLabelText(/change role for grace hopper/i)).not.toBeInTheDocument();
  });

  it("says when a person outranks the caller rather than offering a doomed control", async () => {
    caps.current = capabilities({
      role: "hr_admin",
      permissions: ["member:read", "member:assign_role", "profile:self_read"],
    });
    scriptFetch({
      status: 200,
      body: { items: [employee({ role: "it_admin" })], next_cursor: null },
    });
    renderWithQuery(<EmployeesPage />);

    expect(await screen.findByText("Outranks you")).toBeInTheDocument();
  });

  it("appends the next page under Load more and retires the button on the last one", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [employee()], next_cursor: "cursor-2" } },
      // The page fetches the role catalogue for its practice and seniority filters.
      { status: 200, body: { practices: [], levels: [], titles: [], codes: [] } },
      {
        status: 200,
        body: {
          items: [
            employee({
              id: "88888888-8888-4888-8888-888888888888",
              email: "ada@example.com",
              display_name: "Ada Lovelace",
              jutsu_id: "JUTSU-EMP-BBBBBBBB",
            }),
          ],
          next_cursor: null,
        },
      },
    );
    renderWithQuery(<EmployeesPage />);
    await screen.findByText("Grace Hopper");

    await userEvent.click(screen.getByRole("button", { name: /load more/i }));

    // Appended under the head page, not replacing it — and the button retires once the
    // server says there is nothing older.
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("Grace Hopper")).toBeInTheDocument();
    expect(calledUrl(fetchMock, callIndexFor(fetchMock, "cursor=cursor-2"))).toContain(
      "cursor=cursor-2",
    );
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
  });

  it("renders no role column at all without member:assign_role", async () => {
    caps.current = capabilities({ permissions: ["member:read", "profile:self_read"] });
    scriptFetch({ status: 200, body: { items: [employee()], next_cursor: null } });
    renderWithQuery(<EmployeesPage />);

    await screen.findByText("Grace Hopper");
    expect(screen.queryByText("Change role")).not.toBeInTheDocument();
  });
});

describe("health page", () => {
  it("renders each probe with its verdict", async () => {
    scriptFetch(
      {
        status: 200,
        body: {
          status: "ready",
          checks: { postgres: "ok", neo4j: "not_configured" },
          request_id: "r1",
        },
      },
      { status: 200, body: { by_state: { pending: 2 }, dead_letter: 1, failed_24h: 0 } },
    );
    renderWithQuery(<HealthPage />);

    expect(await screen.findByText("postgres")).toBeInTheDocument();
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect(screen.getByText("not configured")).toBeInTheDocument();
    expect(await screen.findByText(/jobs waiting/i)).toBeInTheDocument();
  });

  it("shows a degraded optional dependency while the service stays ready", async () => {
    // The state this page exists to make visible: the graph is configured and not
    // answering, so retrieval has quietly fallen back to pgvector. Readiness is green
    // because Neo4j is optional (ADR 0022) — if this row rendered neutral too, nothing
    // on the page would distinguish it from "we do not use a graph".
    scriptFetch(
      {
        status: 200,
        body: {
          status: "ready",
          checks: { postgres: "ok", neo4j: "degraded", graph_rag: "degraded" },
          request_id: "r1",
        },
      },
      { status: 200, body: { by_state: {}, dead_letter: 0, failed_24h: 0 } },
    );
    renderWithQuery(<HealthPage />);

    expect(await screen.findByText("neo4j")).toBeInTheDocument();
    // The check's name is rendered verbatim and in mono: it is a key an operator will
    // grep for in `/readyz`, not a sentence.
    expect(screen.getByText("graph_rag")).toBeInTheDocument();
    expect(screen.getAllByText("degraded")).toHaveLength(2);
    expect(screen.getByText("ready")).toBeInTheDocument();
  });

  it("denies without org:read", () => {
    caps.current = capabilities({ permissions: ["profile:self_read"] });
    scriptFetch();
    renderWithQuery(<HealthPage />);

    expect(screen.getByRole("heading", { name: /do not have access/i })).toBeInTheDocument();
  });
});
