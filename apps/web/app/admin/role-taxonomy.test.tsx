import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import EmployeesPage from "@/app/admin/employees/page";
import ProfilePage from "@/app/me/profile/page";
import {
  callIndexFor,
  calledMethod,
  calledUrl,
  capabilities,
  scriptFetch,
  sentBody,
  type Json,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The role taxonomy in the browser.
 *
 * These prove the contract and the courtesies: which URL a filter calls, what the editor
 * sends, and that a control the server would refuse is not offered in the first place.
 * Whether the *server* refuses it is proven in `test_role_taxonomy.py` against real
 * Postgres — a hidden dropdown is a nicety, never the enforcement, and a test here that
 * claimed otherwise would be describing security it cannot see.
 */

const caps: { current: Json } = { current: capabilities() };

vi.mock("@/components/admin/admin-shell", () => ({
  useCapabilities: () => caps.current,
}));

const ADMIN_PERMISSIONS = [
  "org:read",
  "member:read",
  "member:invite",
  "member:assign_role",
  "member:assign_role_code",
  "profile:self_read",
];

beforeEach(() => {
  caps.current = capabilities({ role: "owner", permissions: ADMIN_PERMISSIONS });
});

function catalogue(): Json {
  return {
    practices: [
      { key: "technology", display_name: "Technology", disciplines: [] },
      { key: "audit_assurance", display_name: "Audit & Assurance", disciplines: [] },
    ],
    levels: [
      {
        key: "consultant",
        display_name: "Consultant",
        rank: 30,
        description: "",
        suggested_code: "CON",
      },
      {
        key: "senior_consultant",
        display_name: "Senior Consultant",
        rank: 40,
        description: "",
        suggested_code: "SCN",
      },
    ],
    titles: [
      {
        key: "senior_software_engineer",
        practice_key: "technology",
        discipline_key: null,
        display_name: "Senior Software Engineer",
        level_keys: ["senior_consultant"],
        default_level_key: "senior_consultant",
      },
      {
        key: "audit_senior",
        practice_key: "audit_assurance",
        discipline_key: null,
        display_name: "Audit Senior (Audit In-Charge)",
        level_keys: ["consultant", "senior_consultant"],
        default_level_key: "consultant",
      },
    ],
    codes: [
      {
        code: "SCN",
        display_name: "Senior Consultant",
        tier: 2,
        category: "execution",
        description: "",
        privileged: false,
      },
      {
        code: "CHM",
        display_name: "Chairman / Superadmin",
        tier: 8,
        category: "executive",
        description: "",
        privileged: true,
      },
    ],
  };
}

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
    practice_key: "technology",
    practice: "Technology",
    role_title: "Senior Software Engineer",
    role_level_key: "senior_consultant",
    role_level: "Senior Consultant",
    role_level_rank: 40,
    role_code: "SCN",
    mapping_status: "mapped",
    ...overrides,
  };
}

describe("the employee roster", () => {
  it("shows the real title beside the normalized level, never instead of it", async () => {
    scriptFetch(
      { status: 200, body: { items: [employee()], next_cursor: null } },
      { status: 200, body: catalogue() },
    );
    renderWithQuery(<EmployeesPage />);

    // Comparing people across practices must not rename them. Scoped to the row: the
    // same words legitimately appear in the filter dropdowns above it.
    const row = (await screen.findByRole("rowheader", { name: /Grace Hopper/ })).closest("tr")!;
    expect(within(row).getByText("Senior Software Engineer")).toBeInTheDocument();
    expect(within(row).getByText("Senior Consultant")).toBeInTheDocument();
    expect(within(row).getByText("SCN")).toBeInTheDocument();
  });

  it("says a person needs mapping rather than inventing a level for them", async () => {
    scriptFetch(
      {
        status: 200,
        body: {
          items: [
            employee({
              practice_key: null,
              practice: null,
              role_title: null,
              role_level_key: null,
              role_level: null,
              role_code: null,
              mapping_status: "unmapped",
            }),
          ],
          next_cursor: null,
        },
      },
      { status: 200, body: catalogue() },
    );
    renderWithQuery(<EmployeesPage />);

    expect(await screen.findByText(/needs mapping/i)).toBeInTheDocument();
  });

  it("asks the server for a normalized seniority, which is the Expert Finder query", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [employee()], next_cursor: null } },
      { status: 200, body: catalogue() },
      { status: 200, body: { items: [employee()], next_cursor: null } },
    );
    renderWithQuery(<EmployeesPage />);
    await screen.findByText("Grace Hopper");

    await userEvent.selectOptions(
      await screen.findByLabelText(/seniority/i),
      "senior_consultant",
    );

    await waitFor(() =>
      expect(calledUrl(fetchMock, callIndexFor(fetchMock, "level="))).toContain(
        "level=senior_consultant",
      ),
    );
  });

  it("keeps the filter on the next page rather than silently widening it", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [employee()], next_cursor: "cursor-2" } },
      { status: 200, body: catalogue() },
      { status: 200, body: { items: [employee()], next_cursor: "cursor-2" } },
      { status: 200, body: { items: [], next_cursor: null } },
    );
    renderWithQuery(<EmployeesPage />);
    await screen.findByText("Grace Hopper");
    await userEvent.selectOptions(await screen.findByLabelText(/practice/i), "technology");
    await waitFor(() => expect(callIndexFor(fetchMock, "practice=")).toBeGreaterThan(0));

    await userEvent.click(await screen.findByRole("button", { name: /load more/i }));

    await waitFor(() => {
      const paged = callIndexFor(fetchMock, "cursor=cursor-2");
      expect(calledUrl(fetchMock, paged)).toContain("practice=technology");
    });
  });
});

describe("the assignment editor", () => {
  it("is not offered at all without member:assign_role_code", async () => {
    caps.current = capabilities({
      role: "hr_admin",
      permissions: ["org:read", "member:read", "profile:self_read"],
    });
    scriptFetch(
      { status: 200, body: { items: [employee()], next_cursor: null } },
      { status: 200, body: catalogue() },
    );
    renderWithQuery(<EmployeesPage />);
    await screen.findByText("Grace Hopper");

    expect(screen.queryByRole("button", { name: /role information/i })).not.toBeInTheDocument();
  });

  it("offers only the levels the taxonomy admits for the chosen title", async () => {
    scriptFetch(
      { status: 200, body: { items: [employee()], next_cursor: null } },
      { status: 200, body: catalogue() },
      { status: 404, body: { error: { code: "not_found", message: "none", details: {} }, request_id: "r" } },
    );
    renderWithQuery(<EmployeesPage />);
    await userEvent.click(await screen.findByRole("button", { name: /role information/i }));

    const panel = await screen.findByTestId("role-assignment");
    await userEvent.selectOptions(within(panel).getByLabelText(/^practice$/i), "audit_assurance");
    await userEvent.selectOptions(within(panel).getByLabelText(/role title/i), "audit_senior");

    // Audit Senior maps to two levels in the source, so both are offered and neither is
    // chosen for the administrator.
    const levelSelect = within(panel).getByLabelText(/normalized level/i);
    expect(within(levelSelect).getByRole("option", { name: "Consultant" })).toBeInTheDocument();
    expect(
      within(levelSelect).getByRole("option", { name: "Senior Consultant" }),
    ).toBeInTheDocument();
    expect(within(panel).getByText(/lists two levels for this title/i)).toBeInTheDocument();
  });

  it("does not offer a governance seat to an HR admin", async () => {
    caps.current = capabilities({ role: "hr_admin", permissions: ADMIN_PERMISSIONS });
    scriptFetch(
      { status: 200, body: { items: [employee()], next_cursor: null } },
      { status: 200, body: catalogue() },
      { status: 404, body: { error: { code: "not_found", message: "none", details: {} }, request_id: "r" } },
    );
    renderWithQuery(<EmployeesPage />);
    await userEvent.click(await screen.findByRole("button", { name: /role information/i }));

    const panel = await screen.findByTestId("role-assignment");
    const codeSelect = within(panel).getByLabelText(/platform role code/i);
    expect(within(codeSelect).queryByRole("option", { name: /CHM/ })).not.toBeInTheDocument();
    expect(within(codeSelect).getByRole("option", { name: /SCN/ })).toBeInTheDocument();
    expect(within(panel).getByText(/assigned by an Owner or Super Admin/i)).toBeInTheDocument();
  });

  it("sends exactly the taxonomy fields and no identity", async () => {
    const fetchMock = scriptFetch(
      { status: 200, body: { items: [employee()], next_cursor: null } },
      { status: 200, body: catalogue() },
      { status: 404, body: { error: { code: "not_found", message: "none", details: {} }, request_id: "r" } },
      { status: 200, body: { practice_key: "technology", role_title_key: "senior_software_engineer" } },
      { status: 200, body: { items: [employee()], next_cursor: null } },
    );
    renderWithQuery(<EmployeesPage />);
    await userEvent.click(await screen.findByRole("button", { name: /role information/i }));

    const panel = await screen.findByTestId("role-assignment");
    await userEvent.selectOptions(within(panel).getByLabelText(/^practice$/i), "technology");
    await userEvent.selectOptions(
      within(panel).getByLabelText(/role title/i),
      "senior_software_engineer",
    );
    await userEvent.click(
      within(panel).getByRole("button", { name: /save role information/i }),
    );

    await waitFor(() => expect(callIndexFor(fetchMock, "/role-assignment")).toBeGreaterThan(0));
    const patch = fetchMock.mock.calls.findIndex(
      (call) =>
        String(call[0]).includes("/role-assignment") &&
        (call[1] as RequestInit | undefined)?.method === "PATCH",
    );
    expect(calledMethod(fetchMock, patch)).toBe("PATCH");
    // The tenant and the target are the session's and the path's. Neither is in the body.
    expect(Object.keys(sentBody(fetchMock, patch) as object).sort()).toEqual([
      "practice_key",
      "role_code",
      "role_level_key",
      "role_title_custom",
      "role_title_key",
    ]);
  });
});

describe("the employee's own profile", () => {
  it("shows the assigned role read-only, outside the form they can edit", async () => {
    scriptFetch({
      status: 200,
      body: {
        employee_code: null,
        department: "Engineering",
        designation: "Sr. SWE",
        joining_date: null,
        phone_e164: null,
        skills: [],
        responsibilities: null,
        updated_at: "2026-09-01T10:00:00Z",
        role: {
          practice_key: "technology",
          practice: "Technology",
          discipline: "Software Engineering",
          role_title_key: "senior_software_engineer",
          role_title: "Senior Software Engineer",
          role_level_key: "senior_consultant",
          role_level: "Senior Consultant",
          role_level_rank: 40,
          role_code: "SCN",
          role_code_name: "Senior Consultant",
          role_code_tier: 2,
          mapping_status: "mapped",
        },
      },
    });
    renderWithQuery(<ProfilePage />);

    const card = await screen.findByTestId("assigned-role");
    expect(within(card).getByText("Senior Software Engineer")).toBeInTheDocument();
    expect(within(card).getByText("Senior Consultant")).toBeInTheDocument();
    expect(within(card).getByText(/SCN/)).toBeInTheDocument();
    expect(within(card).getByText(/Technology · Software Engineering/)).toBeInTheDocument();

    // The self-editable form still carries the free-text designation, and nothing in the
    // card is an input.
    expect(screen.getByLabelText(/designation/i)).toHaveValue("Sr. SWE");
    expect(within(card).queryByRole("textbox")).not.toBeInTheDocument();
    expect(within(card).queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("says so plainly when nobody has been placed in the taxonomy yet", async () => {
    scriptFetch({
      status: 200,
      body: {
        employee_code: null,
        department: null,
        designation: null,
        joining_date: null,
        phone_e164: null,
        skills: [],
        responsibilities: null,
        updated_at: "2026-09-01T10:00:00Z",
        role: {
          practice_key: null,
          practice: null,
          discipline: null,
          role_title_key: null,
          role_title: null,
          role_level_key: null,
          role_level: null,
          role_level_rank: null,
          role_code: null,
          role_code_name: null,
          role_code_tier: null,
          mapping_status: "unmapped",
        },
      },
    });
    renderWithQuery(<ProfilePage />);

    const card = await screen.findByTestId("assigned-role");
    expect(
      within(card).getByText(/has not recorded your role information yet/i),
    ).toBeInTheDocument();
  });
});
