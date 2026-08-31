import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import ProfilePage from "@/app/me/profile/page";
import { MEMBER_SECTIONS } from "@/lib/member-nav";
import {
  calledMethod,
  calledUrl,
  envelope,
  type Json,
  pendingFetch,
  scriptFetch,
  sentBody,
} from "@/test-support/api";

/**
 * The profile form, against a scripted `fetch`.
 *
 * The assertions that matter most are about the *request*: this page writes a
 * tenant-scoped row, and the guarantee worth pinning is that the browser cannot name a
 * user or an organisation while doing it.
 */

function profile(overrides: Json = {}): Json {
  return {
    employee_code: "E-1",
    department: "Engineering",
    designation: "Principal",
    joining_date: "2024-04-01",
    phone_e164: "+919876543210",
    skills: ["python", "sql"],
    responsibilities: "Runs the platform.",
    updated_at: "2026-08-31T09:00:00Z",
    ...overrides,
  };
}

const ALLOWED = [
  "department",
  "designation",
  "employee_code",
  "joining_date",
  "phone_e164",
  "responsibilities",
  "skills",
];

beforeEach(() => {
  document.cookie = "";
});

describe("loading", () => {
  it("announces itself while the profile is in flight", async () => {
    pendingFetch({ status: 200, body: profile() });
    render(<ProfilePage />);

    expect(await screen.findByText("Loading your profile")).toBeInTheDocument();
  });

  it("requests the profile endpoint", async () => {
    const fetchMock = scriptFetch({ status: 200, body: profile() });
    render(<ProfilePage />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(calledUrl(fetchMock)).toBe("/api/jutsu/v1/me/profile");
    expect(calledMethod(fetchMock)).toBe("GET");
  });
});

describe("rendering an existing profile", () => {
  it("fills the form from the API", async () => {
    scriptFetch({ status: 200, body: profile() });
    render(<ProfilePage />);

    expect(await screen.findByLabelText("Department")).toHaveValue("Engineering");
    expect(screen.getByLabelText("Designation")).toHaveValue("Principal");
    expect(screen.getByLabelText("Employee code")).toHaveValue("E-1");
    expect(screen.getByLabelText("Phone")).toHaveValue("+919876543210");
  });

  it("renders skills as comma-separated text", async () => {
    scriptFetch({ status: 200, body: profile() });
    render(<ProfilePage />);

    expect(await screen.findByLabelText("Skills")).toHaveValue("python, sql");
  });
});

describe("a missing profile", () => {
  it("is the empty state, not an error", async () => {
    // Migration 0002: an owner is a user row with no profile. A 404 here means "not
    // filled in yet", and rendering it as a failure would teach people that a normal
    // state is broken.
    scriptFetch({ status: 404, body: envelope("not_found", "You do not have a profile yet.") });
    render(<ProfilePage />);

    expect(await screen.findByTestId("profile-empty")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("still offers the form, empty", async () => {
    scriptFetch({ status: 404, body: envelope("not_found", "No profile.") });
    render(<ProfilePage />);

    await screen.findByTestId("profile-empty");
    expect(screen.getByLabelText("Department")).toHaveValue("");
    expect(screen.getByRole("button", { name: /save profile/i })).toBeEnabled();
  });
});

describe("saving", () => {
  it("PATCHes and shows the saved confirmation", async () => {
    const user = userEvent.setup();
    const fetchMock = scriptFetch(
      { status: 200, body: profile() },
      { status: 200, body: profile({ department: "Platform" }) },
    );
    render(<ProfilePage />);

    await user.clear(await screen.findByLabelText("Department"));
    await user.type(screen.getByLabelText("Department"), "Platform");
    await user.click(screen.getByRole("button", { name: /save profile/i }));

    expect(await screen.findByTestId("profile-saved")).toBeInTheDocument();
    expect(calledMethod(fetchMock, 1)).toBe("PATCH");
    expect(calledUrl(fetchMock, 1)).toBe("/api/jutsu/v1/me/profile");
  });

  it("sends only the seven allowed profile fields", async () => {
    const user = userEvent.setup();
    const fetchMock = scriptFetch(
      { status: 200, body: profile() },
      { status: 200, body: profile() },
    );
    render(<ProfilePage />);

    await user.type(await screen.findByLabelText("Department"), "!");
    await user.click(screen.getByRole("button", { name: /save profile/i }));

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
    expect(Object.keys(sentBody(fetchMock, 1)).sort()).toEqual(ALLOWED);
  });

  it.each(["user_id", "org_id", "principals", "tenant", "role", "permissions"])(
    "never sends %s",
    async (field) => {
      const user = userEvent.setup();
      const fetchMock = scriptFetch(
        { status: 200, body: profile() },
        { status: 200, body: profile() },
      );
      render(<ProfilePage />);

      await user.type(await screen.findByLabelText("Department"), "!");
      await user.click(screen.getByRole("button", { name: /save profile/i }));

      await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
      expect(sentBody(fetchMock, 1)).not.toHaveProperty(field);
    },
  );

  it("sends null rather than an empty string when a field is cleared", async () => {
    // "" would store an empty string — a third state alongside unset and has-a-value
    // that nothing else in the system understands.
    const user = userEvent.setup();
    const fetchMock = scriptFetch(
      { status: 200, body: profile() },
      { status: 200, body: profile({ department: null }) },
    );
    render(<ProfilePage />);

    await user.clear(await screen.findByLabelText("Department"));
    await user.click(screen.getByRole("button", { name: /save profile/i }));

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
    expect(sentBody(fetchMock, 1).department).toBeNull();
  });

  it("splits skills back into an array", async () => {
    const user = userEvent.setup();
    const fetchMock = scriptFetch(
      { status: 200, body: profile({ skills: [] }) },
      { status: 200, body: profile() },
    );
    render(<ProfilePage />);

    await user.type(await screen.findByLabelText("Skills"), "python, sql ,  rust");
    await user.click(screen.getByRole("button", { name: /save profile/i }));

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
    expect(sentBody(fetchMock, 1).skills).toEqual(["python", "sql", "rust"]);
  });

  it("disables the button while saving, so a double click cannot double submit", async () => {
    const user = userEvent.setup();
    scriptFetch({ status: 200, body: profile() });
    render(<ProfilePage />);
    await screen.findByLabelText("Department");

    // Second call never resolves: the pending state stays on screen to be asserted.
    pendingFetch({ status: 200, body: profile() });
    await user.type(screen.getByLabelText("Department"), "!");
    await user.click(screen.getByRole("button", { name: /save profile/i }));

    const button = await screen.findByRole("button", { name: /saving/i });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
  });
});

describe("failures", () => {
  it("shows a 422 validation message from the API", async () => {
    const user = userEvent.setup();
    scriptFetch(
      { status: 200, body: profile() },
      {
        status: 422,
        body: envelope("validation_failed", "phone_e164 must match ^\\+[1-9]\\d{1,14}$"),
      },
    );
    render(<ProfilePage />);

    await user.type(await screen.findByLabelText("Phone"), "x");
    await user.click(screen.getByRole("button", { name: /save profile/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/phone_e164 must match/i);
    expect(screen.queryByTestId("profile-saved")).not.toBeInTheDocument();
  });

  it("shows a generic API failure with its request id", async () => {
    const user = userEvent.setup();
    scriptFetch(
      { status: 200, body: profile() },
      { status: 500, body: envelope("internal_error", "Something broke.") },
    );
    render(<ProfilePage />);

    await user.type(await screen.findByLabelText("Department"), "!");
    await user.click(screen.getByRole("button", { name: /save profile/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/something broke/i);
    expect(screen.getByText(/req-abc/i)).toBeInTheDocument();
  });

  it("offers a retry when the initial load fails", async () => {
    scriptFetch({ status: 500, body: envelope("internal_error", "Load failed.") });
    render(<ProfilePage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/load failed/i);
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });
});

describe("cancel", () => {
  it("restores the last saved values", async () => {
    const user = userEvent.setup();
    scriptFetch({ status: 200, body: profile() });
    render(<ProfilePage />);

    const department = await screen.findByLabelText("Department");
    await user.clear(department);
    await user.type(department, "Something else");
    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(screen.getByLabelText("Department")).toHaveValue("Engineering");
  });

  it("is disabled while the form is untouched", async () => {
    scriptFetch({ status: 200, body: profile() });
    render(<ProfilePage />);

    await screen.findByLabelText("Department");
    expect(screen.getByRole("button", { name: /cancel/i })).toBeDisabled();
  });

  it("clears a stale saved confirmation once the form changes again", async () => {
    const user = userEvent.setup();
    scriptFetch(
      { status: 200, body: profile() },
      { status: 200, body: profile() },
    );
    render(<ProfilePage />);

    await user.type(await screen.findByLabelText("Department"), "!");
    await user.click(screen.getByRole("button", { name: /save profile/i }));
    await screen.findByTestId("profile-saved");

    await user.type(screen.getByLabelText("Department"), "?");

    // Leaving "Saved" on screen while the form has changed underneath would be a lie
    // about what is stored.
    expect(screen.queryByTestId("profile-saved")).not.toBeInTheDocument();
  });
});

describe("navigation", () => {
  it("lists Profile as live, pointing at this page", () => {
    const entry = MEMBER_SECTIONS.find((section) => section.href === "/me/profile");

    expect(entry).toBeDefined();
    expect(entry?.status).toBe("live");
    expect(entry?.permission).toBe("profile:self_read");
  });

  it("keeps knowledge transfer pending, and integrations live", () => {
    // The pin moves with the backend. My Integrations went live with migration 0012 +
    // /v1/integrations; Knowledge Transfer still has no KT model and must not have
    // been dragged along with it.
    expect(MEMBER_SECTIONS.find((s) => s.href === "/me/integrations")?.status).toBe("live");
    expect(MEMBER_SECTIONS.find((s) => s.href === "/handover")?.status).toBe("pending");
  });
});
