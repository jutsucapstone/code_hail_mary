import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "sonner";

import IntegrationsPage from "@/app/me/integrations/page";
import {
  calledMethod,
  calledUrl,
  capabilities,
  envelope,
  scriptFetch,
  type Json,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * My Integrations, against a scripted catalogue.
 *
 * The contract under test: each backend state renders as itself (restricted policy,
 * unconfigured deployment, connected account), Connect NAVIGATES to the authorize URL
 * rather than fetching it, and disconnect/sync hit the caller's own endpoints. The
 * security of the flow itself is proven in test_connections.py against real Postgres.
 */

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

// Toast text renders inside the app-level <Toaster>, which these component tests do
// not mount — assert the call, not the portal.
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock("@/components/member/member-shell", () => ({
  useMemberCapabilities: () => capabilities(),
}));

beforeEach(() => {
  vi.restoreAllMocks();
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
    },
  });
}

describe("catalogue states", () => {
  it("renders a restricted provider as policy, not as a dead button", async () => {
    scriptFetch({
      status: 200,
      body: { items: [entry({ allowed: false })] },
    });
    renderWithQuery(<IntegrationsPage />);

    expect(await screen.findByText(/organisation has restricted/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect/i })).not.toBeInTheDocument();
  });

  it("renders an unconfigured provider as deployment state, never a fake Connect", async () => {
    scriptFetch({
      status: 200,
      body: { items: [entry({ configured: false })] },
    });
    renderWithQuery(<IntegrationsPage />);

    expect(await screen.findByText(/not configured for this deployment/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect/i })).not.toBeInTheDocument();
  });

  it("shows the connected account identity and status", async () => {
    scriptFetch({ status: 200, body: { items: [connectedEntry()] } });
    renderWithQuery(<IntegrationsPage />);

    expect(await screen.findByText("ada@slack.example")).toBeInTheDocument();
    expect(screen.getByText("connected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /disconnect/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sync now/i })).toBeInTheDocument();
  });

  it("offers Reconnect when re-authentication is required", async () => {
    scriptFetch({ status: 200, body: { items: [connectedEntry("reauth_required")] } });
    renderWithQuery(<IntegrationsPage />);

    expect(await screen.findByText("reauth required")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reconnect/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sync now/i })).not.toBeInTheDocument();
  });
});

describe("connect", () => {
  it("POSTs to the caller's own endpoint and navigates to the authorize URL", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });

    const fetchMock = scriptFetch(
      { status: 200, body: { items: [entry()] } },
      {
        status: 201,
        body: {
          connection_id: "77777777-7777-4777-8777-777777777777",
          authorize_url: "https://slack.com/oauth/v2/authorize?state=abc",
        },
      },
    );
    renderWithQuery(<IntegrationsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /connect slack/i }));

    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("https://slack.com/oauth/v2/authorize?state=abc"),
    );
    expect(calledUrl(fetchMock, 1)).toBe("/api/jutsu/v1/me/connections/slack");
    expect(calledMethod(fetchMock, 1)).toBe("POST");
  });

  it("surfaces the API's refusal verbatim", async () => {
    scriptFetch(
      { status: 200, body: { items: [entry()] } },
      {
        status: 503,
        body: envelope("service_unavailable", "Slack is not configured for this deployment yet."),
      },
    );
    renderWithQuery(<IntegrationsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /connect slack/i }));

    await waitFor(() =>
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
        "Slack is not configured for this deployment yet.",
      ),
    );
  });
});

describe("disconnect and sync", () => {
  it("DELETEs the caller's own connection", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [connectedEntry()] } },
      { status: 204, body: null },
      { status: 200, body: { items: [entry()] } },
    );
    renderWithQuery(<IntegrationsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /disconnect/i }));

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(calledUrl(fetchMock, 1)).toBe(
      "/api/jutsu/v1/me/connections/77777777-7777-4777-8777-777777777777",
    );
    expect(calledMethod(fetchMock, 1)).toBe("DELETE");
  });

  it("queues a sync through the API", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [connectedEntry()] } },
      { status: 202, body: { job_id: "88888888-8888-4888-8888-888888888888", status: "queued" } },
      { status: 200, body: { items: [connectedEntry()] } },
    );
    renderWithQuery(<IntegrationsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /sync now/i }));

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(calledUrl(fetchMock, 1)).toBe(
      "/api/jutsu/v1/me/connections/77777777-7777-4777-8777-777777777777/sync",
    );
  });
});
