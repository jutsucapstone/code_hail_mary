import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SettingsPage from "@/app/admin/settings/page";
import {
  calledMethod,
  calledUrl,
  capabilities,
  envelope,
  routeFetch,
  sentBody,
  type Json,
  type RoutedResponse,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The nightly sync schedule on the organisation settings page (ADR 0018).
 *
 * What these pin is the contract with the API and the one thing a reader can get wrong:
 * the next run is an instant on the ORGANISATION's clock, so it is rendered in the
 * organisation's zone with that zone named. The schedule's own enforcement — who may
 * write it, and that the tenant comes from the session rather than the body — is proven
 * in `test_sync_schedule.py` against real Postgres, never here.
 *
 * Routed rather than positional throughout: `GET` and `PUT` share one URL, and the org
 * profile shares a prefix with it, so a scripted queue would answer whichever request
 * React's effect order happened to fire first.
 */

const caps: { current: Json } = { current: capabilities() };

vi.mock("@/components/admin/admin-shell", () => ({
  useCapabilities: () => caps.current,
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const ADMIN_PERMISSIONS = [
  "org:read",
  "org:update",
  // The clock has its own permission: Owner, Super Admin, IT Admin and HR Admin hold
  // it, and `org:update` does not reach HR.
  "sync:schedule_manage",
  "member:read",
  "integration:read",
  "integration:self_manage",
  "profile:self_read",
];

/** HR: entitled to set the schedule, not to rename the organisation. */
const HR_PERMISSIONS = [
  "org:read",
  "sync:schedule_manage",
  "member:read",
  "integration:self_manage",
  "profile:self_read",
];

beforeEach(() => {
  vi.restoreAllMocks();
  caps.current = capabilities({ permissions: ADMIN_PERMISSIONS });
});

/** 19:30 UTC is 01:00 the next morning in Asia/Kolkata — the hour the schedule names. */
function schedule(overrides: Json = {}): Json {
  return {
    timezone: "Asia/Kolkata",
    hour_local: 1,
    enabled: true,
    next_sync_at: "2026-09-08T19:30:00Z",
    last_started_at: "2026-09-07T19:30:04Z",
    last_finished_at: "2026-09-07T19:30:09Z",
    last_outcome: "ok",
    last_connections: 4,
    last_enqueued: 3,
    ...overrides,
  };
}

/**
 * The schedule route, listed FIRST wherever the organisation profile is also scripted:
 * `/v1/orgs/current` is a prefix of `/v1/orgs/current/sync-schedule`, so the broader
 * match would otherwise swallow whichever request arrived first.
 */
function scheduleRoute(status = 200, body: Json | null = schedule()): RoutedResponse {
  return { match: "/v1/orgs/current/sync-schedule", status, body };
}

function orgRoute(): RoutedResponse {
  return {
    match: "/v1/orgs/current",
    status: 200,
    body: {
      id: "55555555-5555-4555-8555-555555555555",
      name: "Northwind",
      domain: "northwind.example",
      size_band: "51-200",
      status: "active",
      created_at: "2026-01-01T00:00:00Z",
      members: { total: 3, active: 3, invited: 0, deactivated: 0, admins: 1 },
    },
  };
}

/**
 * The index of the `PUT`.
 *
 * The read and the write share a URL, so `callIndexFor` would always name the read. The
 * method is the only thing that tells them apart.
 */
function putIndex(fetchMock: { mock: { calls: readonly unknown[][] } }): number {
  const index = fetchMock.mock.calls.findIndex(
    (call) => (call[1] as RequestInit | undefined)?.method === "PUT",
  );
  if (index < 0) throw new Error("no PUT was sent");
  return index;
}

function scheduleRequests(fetchMock: { mock: { calls: readonly unknown[][] } }): number {
  return fetchMock.mock.calls.filter((call) =>
    String(call[0]).includes("/v1/orgs/current/sync-schedule"),
  ).length;
}

function section() {
  return screen.getByRole("region", { name: /nightly sync/i });
}

describe("nightly sync", () => {
  it("renders the hour, the zone and the next run on the organisation's clock", async () => {
    routeFetch(scheduleRoute(), orgRoute());
    renderWithQuery(<SettingsPage />);

    expect(await screen.findByText(/^Runs at$/)).toBeInTheDocument();
    const panel = section();
    expect(panel).toHaveTextContent("1:00");
    expect(panel).toHaveTextContent("On");

    // Computed here rather than written out, because the *locale* is the reader's and
    // varies by machine while the *zone* is the organisation's and does not. 19:30 UTC
    // read in Asia/Kolkata is the following calendar day, which is the whole reason the
    // zone has to be applied and then named.
    const expected = new Intl.DateTimeFormat(undefined, {
      timeZone: "Asia/Kolkata",
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date("2026-09-08T19:30:00Z"));
    expect(within(panel).getByText(`${expected}, Asia/Kolkata`)).toBeInTheDocument();
  });

  it("shows how the last run went", async () => {
    routeFetch(scheduleRoute(), orgRoute());
    renderWithQuery(<SettingsPage />);

    const panel = await screen.findByRole("region", { name: /nightly sync/i });
    await waitFor(() => expect(within(panel).getByText("ok")).toBeInTheDocument());
    expect(within(panel).getByText("4")).toBeInTheDocument();
    expect(within(panel).getByText("3")).toBeInTheDocument();
    // The timestamps go through `When`, which renders a machine-readable <time>.
    expect(panel.querySelector("time")).not.toBeNull();
  });

  it("PUTs exactly the three fields the endpoint accepts, then invalidates the schedule", async () => {
    const fetchMock = routeFetch(
      scheduleRoute(),
      orgRoute(),
      scheduleRoute(200, schedule({ hour_local: 2 })),
      scheduleRoute(200, schedule({ hour_local: 2 })),
    );
    renderWithQuery(<SettingsPage />);

    await userEvent.selectOptions(await screen.findByLabelText(/^hour$/i), "2");
    await userEvent.click(screen.getByRole("button", { name: /update schedule/i }));

    const index = await waitFor(() => putIndex(fetchMock));
    expect(calledUrl(fetchMock, index)).toBe("/api/jutsu/v1/orgs/current/sync-schedule");
    expect(calledMethod(fetchMock, index)).toBe("PUT");
    // Exactly these keys. The organisation is the session's, and a body that named one
    // would be an authorization input from the browser.
    expect(sentBody(fetchMock, index)).toEqual({
      timezone: "Asia/Kolkata",
      hour_local: 2,
      enabled: true,
    });
    // The invalidation is observable as the re-read that follows it.
    await waitFor(() => expect(scheduleRequests(fetchMock)).toBeGreaterThanOrEqual(3));
  });

  it("sends enabled: false when the box is unticked", async () => {
    const fetchMock = routeFetch(
      scheduleRoute(),
      orgRoute(),
      scheduleRoute(200, schedule({ enabled: false, next_sync_at: null })),
      scheduleRoute(200, schedule({ enabled: false, next_sync_at: null })),
    );
    renderWithQuery(<SettingsPage />);

    await userEvent.click(await screen.findByLabelText(/sync connected tools automatically/i));
    await userEvent.click(screen.getByRole("button", { name: /update schedule/i }));

    const index = await waitFor(() => putIndex(fetchMock));
    expect(sentBody(fetchMock, index)).toEqual({
      timezone: "Asia/Kolkata",
      hour_local: 1,
      enabled: false,
    });
  });

  it("shows the server's refusal, naming the zone, instead of swallowing it", async () => {
    routeFetch(
      scheduleRoute(),
      orgRoute(),
      scheduleRoute(
        422,
        envelope("validation_failed", "Unknown timezone: Mars/Olympus_Mons."),
      ),
    );
    renderWithQuery(<SettingsPage />);

    await userEvent.click(await screen.findByRole("button", { name: /update schedule/i }));

    expect(await screen.findByText(/unknown timezone: mars\/olympus_mons/i)).toBeInTheDocument();
  });

  it("offers a chosen zone rather than a box that can be typed wrong", async () => {
    routeFetch(scheduleRoute(), orgRoute());
    renderWithQuery(<SettingsPage />);

    const zone = await screen.findByLabelText(/^timezone$/i);
    expect(zone.tagName).toBe("SELECT");
    expect(zone).toHaveValue("Asia/Kolkata");
    expect(within(zone).getByRole("option", { name: "Europe/London" })).toBeInTheDocument();
  });

  it("shows a reader without org:update the facts and no way to change them", async () => {
    // Not a member of any admin role: `integration:self_manage` is what the GET needs,
    // and it is deliberately all this caller has.
    caps.current = capabilities({
      role: "member",
      permissions: ["integration:self_manage", "profile:self_read"],
    });
    routeFetch(scheduleRoute(200, schedule({ last_started_at: null, last_outcome: null })));
    renderWithQuery(<SettingsPage />);

    const panel = await screen.findByRole("region", { name: /nightly sync/i });
    await waitFor(() => expect(panel).toHaveTextContent("Asia/Kolkata"));
    expect(panel).toHaveTextContent("1:00");

    expect(screen.queryByRole("button", { name: /update schedule/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^hour$/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^timezone$/i)).not.toBeInTheDocument();
    // The run history arrives redacted to nulls for this caller, so the page must not
    // read those nulls as a statement that nothing has ever run.
    expect(screen.queryByText(/no automatic run has been recorded/i)).not.toBeInTheDocument();
    // The name form is still the administrator's alone.
    expect(screen.getByRole("heading", { name: /do not have access/i })).toBeInTheDocument();
  });

  it("offers a retry when the schedule itself will not load", async () => {
    routeFetch(
      scheduleRoute(503, envelope("service_unavailable", "The service is not responding.")),
      orgRoute(),
    );
    renderWithQuery(<SettingsPage />);

    expect(await screen.findByText(/the service is not responding/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });
});

describe("who may set the clock", () => {
  it("gives an HR Admin the schedule form without the rename form", async () => {
    // The case that made the permission its own: HR feels a stale corpus first, and
    // `org:update` does not reach them. Gating the schedule on `org:update` left an HR
    // Admin reading a schedule they are entitled to change with no way to change it.
    caps.current = capabilities({ role: "hr_admin", permissions: HR_PERMISSIONS });
    routeFetch(scheduleRoute());
    renderWithQuery(<SettingsPage />);

    expect(await screen.findByLabelText("Hour")).toBeInTheDocument();
    expect(screen.queryByLabelText("Organisation name")).not.toBeInTheDocument();
  });

  it("shows an employee the facts and no form at all", async () => {
    caps.current = capabilities({
      role: "member",
      permissions: ["integration:self_manage", "profile:self_read"],
    });
    routeFetch(scheduleRoute());
    renderWithQuery(<SettingsPage />);

    expect(await screen.findByText(/nightly sync/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Hour")).not.toBeInTheDocument();
  });
});
