import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "sonner";

import IntegrationsPage from "@/app/me/integrations/page";
import {
  calledMethod,
  calledUrl,
  callIndexFor,
  capabilities,
  envelope,
  routeFetch,
  type Json,
  type RoutedResponse,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * My Integrations, against a scripted catalogue.
 *
 * The contract under test: each backend state renders as itself (restricted policy,
 * unconfigured deployment, connected account), Connect NAVIGATES to the authorize URL
 * rather than fetching it, disconnect/sync hit the caller's own endpoints, and BOTH ends
 * of the provider round trip are announced. The security of the flow itself is proven in
 * test_connections.py against real Postgres.
 *
 * Routed rather than positional: the page fires the catalogue and the sync schedule at
 * mount, and effects flush children before parents, so the order those two reach `fetch`
 * is React's business and not something a test should encode. A request no route matches
 * gets a 404 — which is exactly the state the schedule line is required to survive.
 */

// Mutable, because the query string is the whole subject of the round-trip tests: the
// callback's two outcomes differ only in which parameter the browser comes back with.
const nav = vi.hoisted(() => ({ params: new URLSearchParams() }));

vi.mock("next/navigation", () => ({
  useSearchParams: () => nav.params,
}));

// Toast text renders inside the app-level <Toaster>, which these component tests do
// not mount — assert the call, not the portal.
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@/components/member/member-shell", () => ({
  useMemberCapabilities: () => capabilities(),
}));

beforeEach(() => {
  vi.restoreAllMocks();
  // `restoreAllMocks` restores spies; the module mocks above are `vi.fn()`s whose call
  // history would otherwise let one test's toast satisfy the next test's assertion.
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
  nav.params = new URLSearchParams();
});

function entry(overrides: Json = {}): Json {
  return {
    id: "slack",
    name: "Slack",
    group: "communication",
    group_label: "Communication",
    description: "Conversations in channels you are a member of.",
    configured: true,
    allowed: true,
    connection: null,
    ...overrides,
  };
}

function connectedEntry(status = "connected"): Json {
  return entry({
    connection: {
      id: "77777777-7777-4777-8777-777777777777",
      provider: "slack",
      status,
      account_label: "ada@slack.example",
      connected_at: "2026-09-01T10:00:00Z",
      last_sync_at: null,
      last_error_kind: null,
      scopes: ["channels:history", "channels:read", "users:read"],
      document_count: 0,
    },
  });
}

/** A row the callback has already closed out: the attempt ended without a code. */
function refusedEntry(): Json {
  return entry({
    connection: {
      id: "77777777-7777-4777-8777-777777777777",
      provider: "slack",
      status: "error",
      account_label: null,
      connected_at: null,
      last_sync_at: null,
      last_error_kind: "authorization_denied",
      scopes: [],
      document_count: 0,
    },
  });
}

function catalogueRoute(...items: Json[]): RoutedResponse {
  return { match: "/v1/integrations", status: 200, body: { items } };
}

/**
 * The schedule as an employee receives it: the run history redacted to nulls, because
 * `last_connections` is a figure about the organisation and not about them.
 */
function scheduleRoute(overrides: Json = {}): RoutedResponse {
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
      ...overrides,
    },
  };
}

describe("catalogue states", () => {
  it("renders a restricted provider as policy, not as a dead button", async () => {
    routeFetch(catalogueRoute(entry({ allowed: false })));
    renderWithQuery(<IntegrationsPage />);

    expect(await screen.findByText(/organisation has restricted/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect/i })).not.toBeInTheDocument();
  });

  it("renders an unconfigured provider as deployment state, never a fake Connect", async () => {
    routeFetch(catalogueRoute(entry({ configured: false })));
    renderWithQuery(<IntegrationsPage />);

    expect(await screen.findByText(/not configured for this deployment/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect/i })).not.toBeInTheDocument();
  });

  it("shows the connected account identity and status", async () => {
    routeFetch(catalogueRoute(connectedEntry()));
    renderWithQuery(<IntegrationsPage />);

    expect(await screen.findByText("ada@slack.example")).toBeInTheDocument();
    expect(screen.getByText("connected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /disconnect/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sync now/i })).toBeInTheDocument();
  });

  it("offers Reconnect when re-authentication is required", async () => {
    routeFetch(catalogueRoute(connectedEntry("reauth_required")));
    renderWithQuery(<IntegrationsPage />);

    expect(await screen.findByText("reauth required")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reconnect/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sync now/i })).not.toBeInTheDocument();
  });
});

describe("when these tools are read again", () => {
  it("names the next run on the organisation's clock, not the reader's", async () => {
    routeFetch(catalogueRoute(connectedEntry()), scheduleRoute());
    renderWithQuery(<IntegrationsPage />);

    const line = await screen.findByText(/read again automatically/i);
    // 19:30 UTC is 01:00 the next morning in Asia/Kolkata, and the zone is named beside
    // it — a reader in another country must not read their own 01:00 into this.
    expect(line).toHaveTextContent("1:00");
    expect(line).toHaveTextContent("Asia/Kolkata");
  });

  it("says automatic syncing is off, and that a manual sync still works", async () => {
    routeFetch(
      catalogueRoute(connectedEntry()),
      scheduleRoute({ enabled: false, next_sync_at: null }),
    );
    renderWithQuery(<IntegrationsPage />);

    expect(await screen.findByText(/switched off for your organisation/i)).toBeInTheDocument();
    expect(screen.getByText(/sync any connected tool yourself/i)).toBeInTheDocument();
  });

  it("renders nothing at all — and no error — when the schedule cannot be read", async () => {
    routeFetch(catalogueRoute(connectedEntry()), {
      match: "/v1/orgs/current/sync-schedule",
      status: 503,
      body: envelope("service_unavailable", "The service is not responding."),
    });
    renderWithQuery(<IntegrationsPage />);

    // The page's own subject still renders; the supporting line simply is not there.
    expect(await screen.findByText("ada@slack.example")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText(/read again automatically/i)).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(/switched off for your organisation/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("connect", () => {
  it("POSTs to the caller's own endpoint and navigates to the authorize URL", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });

    const fetchMock = routeFetch(catalogueRoute(entry()), scheduleRoute(), {
      match: "/v1/me/connections/slack",
      status: 201,
      body: {
        connection_id: "77777777-7777-4777-8777-777777777777",
        authorize_url: "https://slack.com/oauth/v2/authorize?state=abc",
      },
    });
    renderWithQuery(<IntegrationsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /connect slack/i }));

    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("https://slack.com/oauth/v2/authorize?state=abc"),
    );
    const index = callIndexFor(fetchMock, "/v1/me/connections/slack");
    expect(calledUrl(fetchMock, index)).toBe("/api/jutsu/v1/me/connections/slack");
    expect(calledMethod(fetchMock, index)).toBe("POST");
  });

  it("surfaces the API's refusal verbatim", async () => {
    routeFetch(catalogueRoute(entry()), scheduleRoute(), {
      match: "/v1/me/connections/slack",
      status: 503,
      body: envelope("service_unavailable", "Slack is not configured for this deployment yet."),
    });
    renderWithQuery(<IntegrationsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /connect slack/i }));

    await waitFor(() =>
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
        "Slack is not configured for this deployment yet.",
      ),
    );
  });
});

describe("returning from the provider", () => {
  it("announces a completed connection and clears the parameter", async () => {
    nav.params = new URLSearchParams("connected=slack");
    const replaceState = vi.spyOn(window.history, "replaceState");
    routeFetch(catalogueRoute(connectedEntry()), scheduleRoute());
    renderWithQuery(<IntegrationsPage />);

    await waitFor(() =>
      expect(vi.mocked(toast.success)).toHaveBeenCalledWith(
        "Connected. JUTSU can now see what slack lets your account see.",
      ),
    );
    expect(replaceState).toHaveBeenCalledWith(null, "", "/me/integrations");
  });

  it("names the tool that was not connected, and claims no cause for it", async () => {
    nav.params = new URLSearchParams("connect_error=slack");
    const replaceState = vi.spyOn(window.history, "replaceState");
    routeFetch(catalogueRoute(refusedEntry()), scheduleRoute());
    renderWithQuery(<IntegrationsPage />);

    await waitFor(() =>
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
        "Slack was not connected. The authorisation did not complete, and nothing was read from it.",
      ),
    );
    // A denial must leave the URL as clean as a success does — a reload of a page still
    // carrying `?connect_error=` would announce a refusal that already happened.
    expect(replaceState).toHaveBeenCalledWith(null, "", "/me/integrations");
  });

  it("falls back to a generic sentence when the reason names no connector", async () => {
    // `_deny_reason` sanitises the provider's own error code when the abandoned attempt
    // matched no row of the caller's — so this value is not a tool name and must not be
    // printed as one.
    nav.params = new URLSearchParams("connect_error=access_denied");
    routeFetch(catalogueRoute(entry()), scheduleRoute());
    renderWithQuery(<IntegrationsPage />);

    await waitFor(() =>
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
        "That connection was not completed. Nothing was connected and nothing was read.",
      ),
    );
  });

  it("does not blame a sync that never ran", async () => {
    routeFetch(catalogueRoute(refusedEntry()), scheduleRoute());
    renderWithQuery(<IntegrationsPage />);

    expect(
      await screen.findByText(/the authorisation was not completed, so nothing was connected/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/last sync problem/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reconnect/i })).toBeInTheDocument();
  });
});

describe("disconnect and sync", () => {
  it("DELETEs the caller's own connection", async () => {
    const fetchMock = routeFetch(
      catalogueRoute(connectedEntry()),
      scheduleRoute(),
      {
        match: "/v1/me/connections/77777777-7777-4777-8777-777777777777",
        status: 204,
        body: null,
      },
      catalogueRoute(entry()),
    );
    renderWithQuery(<IntegrationsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /disconnect/i }));

    const index = await waitFor(() =>
      callIndexFor(fetchMock, "/v1/me/connections/77777777-7777-4777-8777-777777777777"),
    );
    expect(calledUrl(fetchMock, index)).toBe(
      "/api/jutsu/v1/me/connections/77777777-7777-4777-8777-777777777777",
    );
    expect(calledMethod(fetchMock, index)).toBe("DELETE");
  });

  it("queues a sync through the API", async () => {
    const fetchMock = routeFetch(
      catalogueRoute(connectedEntry()),
      scheduleRoute(),
      {
        match: "/v1/me/connections/77777777-7777-4777-8777-777777777777/sync",
        status: 202,
        body: { job_id: "88888888-8888-4888-8888-888888888888", status: "queued" },
      },
      catalogueRoute(connectedEntry()),
    );
    renderWithQuery(<IntegrationsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /sync now/i }));

    const index = await waitFor(() =>
      callIndexFor(fetchMock, "/v1/me/connections/77777777-7777-4777-8777-777777777777/sync"),
    );
    expect(calledUrl(fetchMock, index)).toBe(
      "/api/jutsu/v1/me/connections/77777777-7777-4777-8777-777777777777/sync",
    );
  });
});

describe("a connection that stalled part-way", () => {
  it("offers a way forward from 'connecting', not only Disconnect", async () => {
    routeFetch(catalogueRoute(connectedEntry("connecting")));
    renderWithQuery(<IntegrationsPage />);

    // Reachable by closing the provider's consent screen — no mistake required — and
    // the previous UI left exactly one button on the row, which throws the attempt away.
    expect(
      await screen.findByRole("button", { name: "Continue connecting" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disconnect" })).toBeInTheDocument();
  });

  it("still calls it Reconnect once the callback has recorded a failure", async () => {
    routeFetch(catalogueRoute(connectedEntry("error")));
    renderWithQuery(<IntegrationsPage />);

    expect(await screen.findByRole("button", { name: "Reconnect" })).toBeInTheDocument();
  });
});
