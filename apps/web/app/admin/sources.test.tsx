import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import SourcesPage from "@/app/admin/sources/page";
import {
  calledMethod,
  calledUrl,
  callIndexFor,
  capabilities,
  envelope,
  routeFetch,
  scriptFetch,
  type Json,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * Knowledge sources, and the one thing an administrator can now do about a stalled one.
 *
 * What these prove is the contract the browser holds up: which URL the control calls,
 * which method, that the caches it changed are invalidated, and that the button is
 * absent for a caller who may watch but not act. Whether the API enforces any of that
 * is proven in `test_admin_operations.py` against real Postgres — never here.
 */

const caps: { current: Json } = { current: capabilities() };

vi.mock("@/components/admin/admin-shell", () => ({
  useCapabilities: () => caps.current,
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const SOURCE_ID = "11111111-1111-4111-8111-111111111111";

function source(overrides: Json = {}): Json {
  return {
    id: SOURCE_ID,
    system: "local",
    provider: null,
    account_label: null,
    status: "idle",
    last_sync_at: "2026-09-01T10:00:00Z",
    document_count: 12,
    jobs_pending: 0,
    jobs_completed: 12,
    jobs_failed: 1,
    last_walk: {},
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  caps.current = capabilities({
    permissions: [
      "org:read",
      "integration:read",
      "integration:connect",
      "profile:self_read",
      "retrieval:query",
    ],
  });
});

describe("knowledge sources", () => {
  it("renders a source with what it has produced", async () => {
    scriptFetch({ status: 200, body: { items: [source()] } });
    renderWithQuery(<SourcesPage />);

    expect(await screen.findByRole("cell", { name: "Local corpus" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveTextContent("12");
  });

  it("explains an empty list rather than showing a blank table", async () => {
    scriptFetch({ status: 200, body: { items: [] } });
    renderWithQuery(<SourcesPage />);

    expect(await screen.findByText(/no knowledge sources yet/i)).toBeInTheDocument();
  });

  it("queues a re-sync with a POST to that source, and says what will happen", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [source()] } },
      { status: 202, body: { job_id: "22222222-2222-4222-8222-222222222222" } },
      { status: 200, body: { items: [source({ jobs_pending: 1 })] } },
    );
    renderWithQuery(<SourcesPage />);
    await screen.findByRole("cell", { name: "Local corpus" });

    await userEvent.click(screen.getByRole("button", { name: /re-sync local corpus/i }));

    const index = await waitFor(() => callIndexFor(fetchMock, `/v1/sources/${SOURCE_ID}/sync`));
    expect(calledMethod(fetchMock, index)).toBe("POST");
    expect(calledUrl(fetchMock, index)).toContain(`/v1/sources/${SOURCE_ID}/sync`);
    // A single click with no confirmation: the server holds one walk per source, so
    // there is nothing to guard against.
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(expect.stringContaining("Re-sync queued for Local corpus")),
    );
  });

  it("invalidates the sources and jobs caches it changed", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [source()] } },
      { status: 202, body: { job_id: "22222222-2222-4222-8222-222222222222" } },
      { status: 200, body: { items: [source({ jobs_pending: 1 })] } },
    );
    const { client } = renderWithQuery(<SourcesPage />);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await screen.findByRole("cell", { name: "Local corpus" });

    await userEvent.click(screen.getByRole("button", { name: /re-sync local corpus/i }));

    // The Jobs page is the other surface a queued walk changes, and it mounts no query
    // here — so the call is what there is to observe. A stale Jobs page is how an
    // administrator concludes the button did nothing.
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["jobs"] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["sources"] });
    // And the observable half: this table re-reads rather than leaving counters from
    // before the walk was queued.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(calledMethod(fetchMock, 2)).toBe("GET");
    expect(calledUrl(fetchMock, 2)).toContain("/v1/sources");
  });

  it("says the click landed while the request is in flight", async () => {
    // A control with no pending state invites a second click at the one moment the
    // person has no evidence the first did anything.
    routeFetch(
      { match: "/sync", status: 202, body: null, pending: true },
      { match: "/v1/sources", status: 200, body: { items: [source()] } },
    );
    renderWithQuery(<SourcesPage />);
    await screen.findByRole("cell", { name: "Local corpus" });

    await userEvent.click(screen.getByRole("button", { name: /re-sync local corpus/i }));

    const button = await screen.findByRole("button", { name: /re-sync local corpus/i });
    await waitFor(() => expect(button).toBeDisabled());
    expect(button).toHaveTextContent(/queueing/i);
  });

  it("surfaces a refusal instead of a silent no-op", async () => {
    scriptFetch(
      { status: 200, body: { items: [source()] } },
      { status: 403, body: envelope("forbidden", "Your role does not include that.") },
    );
    renderWithQuery(<SourcesPage />);
    await screen.findByRole("cell", { name: "Local corpus" });

    await userEvent.click(screen.getByRole("button", { name: /re-sync local corpus/i }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
  });

  it("offers no control to a caller who may watch but not act", async () => {
    caps.current = capabilities({
      permissions: ["org:read", "integration:read", "profile:self_read"],
    });
    scriptFetch({ status: 200, body: { items: [source()] } });
    renderWithQuery(<SourcesPage />);
    await screen.findByRole("cell", { name: "Local corpus" });

    expect(screen.queryByRole("button", { name: /re-sync/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Actions" })).not.toBeInTheDocument();
  });

  it("denies without integration:read instead of rendering an empty page", () => {
    caps.current = capabilities({ permissions: ["org:read", "profile:self_read"] });
    scriptFetch();
    renderWithQuery(<SourcesPage />);

    expect(screen.getByRole("heading", { name: /do not have access/i })).toBeInTheDocument();
  });
});

describe("telling one source from another", () => {
  it("names the provider, not the ACL namespace it shares with three others", async () => {
    scriptFetch({
      status: 200,
      body: {
        items: [
          source({ id: SOURCE_ID, system: "gmail", provider: "google_drive", account_label: "ada@acme.com" }),
        ],
      },
    });
    renderWithQuery(<SourcesPage />);

    // `system` is `gmail` for Drive, Gmail, Calendar and Meet alike, so a table that
    // showed it gave an administrator four identical rows.
    expect(await screen.findByRole("cell", { name: "Google Drive" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "ada@acme.com" })).toBeInTheDocument();
    expect(screen.queryByRole("cell", { name: "gmail" })).not.toBeInTheDocument();
  });

  it("names the provider and the account in the re-sync control", async () => {
    scriptFetch({
      status: 200,
      body: {
        items: [source({ system: "gmail", provider: "gmail", account_label: "ada@acme.com" })],
      },
    });
    renderWithQuery(<SourcesPage />);

    expect(
      await screen.findByRole("button", { name: "Re-sync Gmail for ada@acme.com" }),
    ).toBeInTheDocument();
  });

  it("says so when the filters match nothing, instead of an empty table", async () => {
    // Two rows whose system and status do not overlap, so a cross-filter matches none.
    scriptFetch({
      status: 200,
      body: {
        items: [
          source({ id: SOURCE_ID, system: "gmail", provider: "gmail", status: "idle" }),
          source({
            id: "22222222-2222-4222-8222-222222222222",
            system: "slack",
            provider: "slack",
            status: "error",
          }),
        ],
      },
    });
    renderWithQuery(<SourcesPage />);
    await screen.findByRole("cell", { name: "Gmail" });

    await userEvent.selectOptions(screen.getByLabelText("System"), "gmail");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "error");

    // The page's own EmptyState covers "nothing connected". This is the far more
    // common case, which used to render a table header with nothing under it — and an
    // administrator cannot tell that from a failed load.
    expect(await screen.findByText(/no sources match those filters/i)).toBeInTheDocument();
    expect(screen.queryByRole("cell", { name: "Gmail" })).not.toBeInTheDocument();
  });
});
