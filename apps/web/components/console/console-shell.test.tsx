import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConsoleShell, type ShellSection } from "@/components/console/console-shell";
import { capabilities, envelope, scriptFetch } from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/admin",
}));

beforeEach(() => {
  replace.mockClear();
});

const SECTIONS: ShellSection[] = [
  {
    href: "/admin",
    name: "Overview",
    description: "At a glance.",
    status: "live",
    slice: "P1",
    permission: "org:read",
  },
  {
    href: "/admin/audit",
    name: "Audit log",
    description: "Every security-sensitive action.",
    status: "pending",
    slice: "P2",
    permission: "audit:read",
    group: "Operations",
  },
  {
    href: "/admin/secrets",
    name: "Deletion",
    description: "Only an owner may.",
    status: "live",
    slice: "P1",
    permission: "org:delete",
  },
];

function shell(children: React.ReactNode = <p>Console body</p>) {
  return (
    <ConsoleShell sections={SECTIONS} navLabel="Admin sections" variant="sidebar">
      {children}
    </ConsoleShell>
  );
}

describe("while the caller is being identified", () => {
  it("announces itself rather than rendering in silence", () => {
    scriptFetch({ status: 200, body: capabilities() });
    renderWithQuery(shell());

    expect(screen.getByRole("status")).toHaveTextContent(/loading your access/i);
  });

  it("does not render the page body before the principal is known", () => {
    // Children reading `useConsoleCapabilities()` would throw on a null context. The
    // shell's contract is that they never see one.
    scriptFetch({ status: 200, body: capabilities() });
    renderWithQuery(shell());

    expect(screen.queryByText("Console body")).not.toBeInTheDocument();
  });
});

describe("once identified", () => {
  it("renders the page body", async () => {
    scriptFetch({ status: 200, body: capabilities() });
    renderWithQuery(shell());

    expect(await screen.findByText("Console body")).toBeInTheDocument();
  });

  it("asks who the caller is exactly once per mount", async () => {
    // The point of the query cache. Both shells used to hold their own useState/useEffect
    // copy of this fetch and re-ran it on every navigation.
    const fetchMock = scriptFetch({ status: 200, body: capabilities() });
    renderWithQuery(shell());

    await screen.findByText("Console body");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/jutsu/v1/me");
  });

  it("shows the JUTSU ID, never an email — the header carries no PII", async () => {
    scriptFetch({ status: 200, body: capabilities() });
    renderWithQuery(shell());

    expect(await screen.findByText("JUTSU-ADM-9HXPNFG8")).toBeInTheDocument();
  });

  it("gives the page a main landmark for the skip link to reach", async () => {
    // The id used to sit on a div in the route layout that wrapped the whole shell —
    // header and nav included — so "Skip to main content" skipped nothing, and the
    // employee console had no <main> at all. That is a WCAG 2.4.1 failure which presents
    // as a dead key press.
    scriptFetch({ status: 200, body: capabilities() });
    renderWithQuery(shell());

    // Wait on the body, not on `main`: the landmark is rendered from the first frame so
    // that the loading region sits inside it, and awaiting it would resolve before the
    // principal arrives.
    const body = await screen.findByText("Console body");
    const main = screen.getByRole("main");
    expect(main).toHaveAttribute("id", "main-content");
    expect(main).toContainElement(body);
    // The navigation is a sibling of main, not inside it — otherwise skipping to main
    // would still land above the section list.
    expect(main).not.toContainElement(screen.getByRole("navigation", { name: "Admin sections" }));
  });
});

describe("navigation", () => {
  it("hides sections the caller has no permission for", async () => {
    // A courtesy, not the enforcement: the endpoint behind each re-checks server-side.
    scriptFetch({ status: 200, body: capabilities() });
    renderWithQuery(shell());

    await screen.findByText("Console body");
    expect(screen.getByRole("link", { name: "Overview" })).toBeInTheDocument();
    // `org:delete` is not in the fixture's permission set.
    expect(screen.queryByText("Deletion")).not.toBeInTheDocument();
  });

  it("lists a pending section without linking it", async () => {
    scriptFetch({ status: 200, body: capabilities({ permissions: ["org:read", "audit:read"] }) });
    renderWithQuery(shell());

    await screen.findByText("Console body");
    expect(screen.getByText("Audit log")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /audit log/i })).not.toBeInTheDocument();
  });

  it("renders the IA heading for a grouped section", async () => {
    scriptFetch({ status: 200, body: capabilities({ permissions: ["org:read", "audit:read"] }) });
    renderWithQuery(shell());

    await screen.findByText("Console body");
    expect(screen.getByRole("heading", { name: "Operations" })).toBeInTheDocument();
  });
});

describe("an unusable session", () => {
  it("sends a 401 to sign-in", async () => {
    scriptFetch({ status: 401, body: envelope("unauthenticated", "Not signed in.") });
    renderWithQuery(shell());

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/signin"));
  });

  it("does not flash an error on the way out", async () => {
    scriptFetch({ status: 401, body: envelope("unauthenticated", "Not signed in.") });
    renderWithQuery(shell());

    await waitFor(() => expect(replace).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("a failure that is not about identity", () => {
  it("does NOT send a valid session to sign-in", async () => {
    // The regression this replaces: both shells redirected on *any* rejection, so a
    // transient 503 on /v1/me bounced a perfectly good session to the sign-in page —
    // where signing in succeeds, lands back here, and fails again. A dependency being
    // down is not a reason to doubt who somebody is.
    scriptFetch({ status: 503, body: envelope("service_unavailable", "Database is down.") });
    renderWithQuery(shell());

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("says what went wrong and keeps the reader where they are", async () => {
    scriptFetch({ status: 503, body: envelope("service_unavailable", "Database is down.") });
    renderWithQuery(shell());

    expect(await screen.findByText("Database is down.")).toBeInTheDocument();
    expect(screen.getByText(/req-abc/)).toBeInTheDocument();
  });

  it("offers a way out, and it works", async () => {
    // Without this the only escape from a blipped API was reloading the page by hand.
    const fetchMock = scriptFetch(
      { status: 503, body: envelope("service_unavailable", "Database is down.") },
      { status: 200, body: capabilities() },
    );
    renderWithQuery(shell());

    await userEvent.click(await screen.findByRole("button", { name: /try again/i }));

    expect(await screen.findByText("Console body")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("renders a 403 as a denial rather than as a load failure", async () => {
    // A 403 on `/v1/me` should never read as "that did not load": the session is fine and
    // retrying cannot change the answer.
    scriptFetch({ status: 403, body: envelope("forbidden", "Not permitted.") });
    renderWithQuery(shell());

    expect(await screen.findByRole("heading", { name: /do not have access/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });
});
