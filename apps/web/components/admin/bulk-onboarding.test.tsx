import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { BulkOnboarding } from "@/components/admin/bulk-onboarding";
import { calledUrl, envelope, routeFetch, scriptFetch, sentBody, type Json } from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The bulk onboarding panel, against a scripted API.
 *
 * What these prove is the contract and the guard rails a person actually feels: that
 * checking a list sends nothing, that the button's number matches the rows the request
 * carries, and that an unticked row is left out. Whether the API refuses a role or a
 * duplicate is proven by `test_bulk_invitations.py` against real Postgres — never here,
 * because a scripted `fetch` will agree with whatever the component asks it.
 */

const GRANTABLE = ["hr_admin", "analyst", "viewer", "member"] as const;

function row(overrides: Json = {}): Json {
  return {
    email: "ada@example.com",
    role: "member",
    role_title: null,
    outcome: "ready",
    detail: "Will be invited.",
    ...overrides,
  };
}

/**
 * Put a file on the input, past the `accept` attribute.
 *
 * `applyAccept: false`, because `accept` is a hint to the file picker and nothing more —
 * a file arrives regardless of it by drag-and-drop, which is precisely why the component
 * checks the name itself. Leaving the library's filter on would test the browser's
 * courtesy and never reach the guard that matters.
 */
async function upload(file: File) {
  await userEvent.upload(screen.getByLabelText(/Drop a CSV or Excel file/), file, {
    applyAccept: false,
  });
}

function renderPanel(onInvited = vi.fn()) {
  renderWithQuery(<BulkOnboarding grantable={[...GRANTABLE]} onInvited={onInvited} />);
  return { onInvited };
}

describe("checking a list", () => {
  it("sends the pasted addresses to the preview route and nothing else", async () => {
    const fetchMock = scriptFetch({
      status: 200,
      body: { rows: [row()], ready: 1, total: 1 },
    });
    renderPanel();

    await userEvent.type(
      screen.getByLabelText("Work emails"),
      "ada@example.com babbage@example.com",
    );
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));

    await screen.findByRole("table");
    // One request, and it is the one that writes nothing. A preview that also invited
    // would make the whole two-step design pointless.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(calledUrl(fetchMock)).toContain("/v1/employees/invitations/preview");
    expect(sentBody(fetchMock)).toEqual({
      emails: "ada@example.com babbage@example.com",
      role: "member",
    });
  });

  it("cannot be checked until there is something to check", () => {
    renderPanel();

    expect(screen.getByRole("button", { name: "Check the list" })).toBeDisabled();
  });

  it("carries the chosen default role", async () => {
    const fetchMock = scriptFetch({
      status: 200,
      body: { rows: [row({ role: "viewer" })], ready: 1, total: 1 },
    });
    renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "ada@example.com");
    await userEvent.selectOptions(screen.getByLabelText("Role for everyone"), "viewer");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));

    await screen.findByRole("table");
    expect(sentBody(fetchMock)).toMatchObject({ role: "viewer" });
  });

  it("posts a CSV file as text rather than as an upload", async () => {
    // No multipart handler exists, and none is needed: the browser already has the text.
    const fetchMock = scriptFetch({
      status: 200,
      body: { rows: [row()], ready: 1, total: 1 },
    });
    renderPanel();

    const file = new File(["email,role\nada@example.com,member\n"], "team.csv", {
      type: "text/csv",
    });
    await upload(file);
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));

    await screen.findByRole("table");
    expect(sentBody(fetchMock)).toEqual({
      csv: "email,role\nada@example.com,member\n",
      role: "member",
    });
  });

  it("posts a workbook as base64, because a zip of XML is not text", async () => {
    const fetchMock = scriptFetch({
      status: 200,
      body: { rows: [row()], ready: 1, total: 1 },
    });
    renderPanel();

    // Bytes that are not valid UTF-8, which is the whole reason this path exists — a
    // `.xlsx` read as text would arrive mangled and the server would refuse it.
    const bytes = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0xff, 0xfe, 0x00, 0x80]);
    await upload(new File([bytes], "team.xlsx"));
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));

    await screen.findByRole("table");
    const body = sentBody(fetchMock) as { xlsx_base64: string; role: string };
    expect(body.role).toBe("member");
    expect([...atob(body.xlsx_base64)].map((c) => c.charCodeAt(0))).toEqual([...bytes]);
  });

  it("refuses a file it cannot possibly parse, before any request", async () => {
    const fetchMock = scriptFetch({ status: 200, body: { rows: [], ready: 0, total: 0 } });
    renderPanel();

    const file = new File(["%PDF-1.7"], "handbook.pdf", { type: "application/pdf" });
    await upload(file);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "handbook.pdf is not a CSV or Excel file.",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the API's refusal rather than a generic failure", async () => {
    routeFetch({
      match: "/preview",
      status: 422,
      body: envelope("validation_failed", "That is 201 addresses. Import up to 200 at a time."),
    });
    renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "ada@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Import up to 200 at a time");
  });
});

describe("reading the preview", () => {
  it("shows every row with the outcome the reader can act on", async () => {
    scriptFetch({
      status: 200,
      body: {
        rows: [
          row(),
          row({ email: "here@example.com", outcome: "already_member", detail: "Already in this organisation." }),
          row({ email: "nope", outcome: "invalid_email", detail: "Not an email address." }),
        ],
        ready: 1,
        total: 3,
      },
    });
    renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));

    const table = await screen.findByRole("table");
    expect(within(table).getByText("Will invite")).toBeInTheDocument();
    expect(within(table).getByText("Already here")).toBeInTheDocument();
    expect(within(table).getByText("Not an address")).toBeInTheDocument();
    // The count the reader sees, and the count the button promises, come from the same
    // filtered list — so a row that will not be invited cannot inflate either.
    expect(screen.getByRole("button", { name: "Send 1 invitation" })).toBeInTheDocument();
  });

  it("says so when a file parses to nothing, rather than showing an empty table", async () => {
    // A spreadsheet exported with only a header row. The blank table under the heading
    // "Preview" that this replaces read as a broken page rather than as an answer.
    scriptFetch({ status: 200, body: { rows: [], ready: 0, total: 0 } });
    renderPanel();

    await upload(new File(["email\n"], "empty.csv", { type: "text/csv" }));
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));

    expect(await screen.findByText("No addresses found")).toBeInTheDocument();
    expect(screen.getByText(/empty\.csv was read/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("points a paste with no addresses at the right fix", async () => {
    scriptFetch({ status: 200, body: { rows: [], ready: 0, total: 0 } });
    renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "   ,,,   ");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));

    expect(await screen.findByText("No addresses found")).toBeInTheDocument();
    expect(screen.getByText(/Paste one address per line/)).toBeInTheDocument();
  });

  it("offers no way to tick a row that cannot be invited", async () => {
    scriptFetch({
      status: 200,
      body: {
        rows: [row({ email: "here@example.com", outcome: "already_member", detail: "x" })],
        ready: 0,
        total: 1,
      },
    });
    renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));

    await screen.findByRole("table");
    expect(screen.getByLabelText("Invite here@example.com")).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^Send/ })).not.toBeInTheDocument();
  });
});

describe("sending", () => {
  it("sends only the rows still ticked, and says so on the button", async () => {
    const fetchMock = routeFetch(
      {
        match: "/preview",
        status: 200,
        body: {
          rows: [row(), row({ email: "babbage@example.com" })],
          ready: 2,
          total: 2,
        },
      },
      {
        match: "/bulk",
        status: 202,
        body: { rows: [row({ outcome: "sent", detail: "Invitation sent." })], sent: 1, failed: 0 },
      },
    );
    renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));
    await screen.findByRole("table");
    await userEvent.click(screen.getByLabelText("Invite babbage@example.com"));

    // The label follows the ticks, because a button that says "Send 2" while sending one
    // is the failure this whole panel exists to avoid.
    const send = screen.getByRole("button", { name: "Send 1 invitation" });
    await userEvent.click(send);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(sentBody(fetchMock, 1)).toEqual({
      rows: [{ email: "ada@example.com", role: "member", role_title: null }],
    });
  });

  it("carries a role the reader changed on the row", async () => {
    const fetchMock = routeFetch(
      { match: "/preview", status: 200, body: { rows: [row()], ready: 1, total: 1 } },
      {
        match: "/bulk",
        status: 202,
        body: { rows: [row({ outcome: "sent", role: "analyst" })], sent: 1, failed: 0 },
      },
    );
    renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));
    await screen.findByRole("table");
    await userEvent.selectOptions(screen.getByLabelText("Role for ada@example.com"), "analyst");
    await userEvent.click(screen.getByRole("button", { name: "Send 1 invitation" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(sentBody(fetchMock, 1)).toEqual({
      rows: [{ email: "ada@example.com", role: "analyst", role_title: null }],
    });
  });

  it("reports each row's fate in place, keeping the rows it never asked about", async () => {
    routeFetch(
      {
        match: "/preview",
        status: 200,
        body: {
          rows: [
            row(),
            row({ email: "here@example.com", outcome: "already_member", detail: "Already in this organisation." }),
          ],
          ready: 1,
          total: 2,
        },
      },
      {
        match: "/bulk",
        status: 202,
        body: {
          rows: [row({ outcome: "failed", detail: "The invitation could not be emailed." })],
          sent: 0,
          failed: 1,
        },
      },
    );
    const { onInvited } = renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));
    await screen.findByRole("table");
    await userEvent.click(screen.getByRole("button", { name: "Send 1 invitation" }));

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    // The row the send was never asked about survives: the reader is looking at their
    // whole list, and dropping the rows the API did not answer about would read as data
    // loss.
    expect(screen.getByText("here@example.com")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("0 invited, 1 could not be");
    expect(onInvited).toHaveBeenCalled();
  });

  it("does not re-select the rows the reader unticked", async () => {
    // **The bug this exists for.** `send()` used to clear `excluded` afterwards while
    // `mergeResults` only overwrote the rows it had actually requested — so every
    // deliberately unticked row still read `ready`, was silently re-selected, and the
    // tick column had been unmounted so there was no way to remove it again. The button
    // came back offering to invite exactly the people the administrator had removed.
    const fetchMock = routeFetch(
      {
        match: "/preview",
        status: 200,
        body: {
          rows: [row(), row({ email: "contractor@example.com" })],
          ready: 2,
          total: 2,
        },
      },
      {
        match: "/bulk",
        status: 202,
        body: {
          rows: [row({ outcome: "sent", detail: "Invitation sent." })],
          sent: 1,
          failed: 0,
        },
      },
    );
    renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));
    await screen.findByRole("table");
    await userEvent.click(screen.getByLabelText("Invite contractor@example.com"));
    await userEvent.click(screen.getByRole("button", { name: "Send 1 invitation" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    // The contractor stays out. No button offers to send them.
    expect(screen.queryByRole("button", { name: /^Send/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Retry/ })).not.toBeInTheDocument();
  });

  it("keeps the tick column after a send, so a retry can still be steered", async () => {
    routeFetch(
      { match: "/preview", status: 200, body: { rows: [row()], ready: 1, total: 1 } },
      {
        match: "/bulk",
        status: 202,
        body: {
          rows: [row({ outcome: "failed", detail: "Could not be emailed." })],
          sent: 0,
          failed: 1,
        },
      },
    );
    renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));
    await screen.findByRole("table");
    await userEvent.click(screen.getByRole("button", { name: "Send 1 invitation" }));

    // A failed row is retryable, and the checkbox that controls it still exists.
    expect(await screen.findByRole("button", { name: "Retry 1 row" })).toBeInTheDocument();
    expect(screen.getByLabelText("Retry ada@example.com")).toBeEnabled();
  });

  it("offers no retry when every row went through", async () => {
    routeFetch(
      { match: "/preview", status: 200, body: { rows: [row()], ready: 1, total: 1 } },
      {
        match: "/bulk",
        status: 202,
        body: { rows: [row({ outcome: "sent", detail: "Invitation sent." })], sent: 1, failed: 0 },
      },
    );
    renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));
    await screen.findByRole("table");
    await userEvent.click(screen.getByRole("button", { name: "Send 1 invitation" }));

    expect(await screen.findByText("Invited")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Send|Retry)/ })).not.toBeInTheDocument();
  });

  it("retries only the failed row, and says so", async () => {
    // Retrying is meant to be pressed. A failed row had its invitation revoked
    // server-side precisely so the address is free again, and the API answers
    // `already_invited` for anyone who did get mail — so a second press cannot email
    // anybody twice.
    //
    // The scripted `/bulk` body here uses outcomes `invite_many` can actually emit. The
    // version this replaced returned `outcome: "ready"`, which the API never produces,
    // so it agreed with the component instead of testing it.
    const fetchMock = routeFetch(
      {
        match: "/preview",
        status: 200,
        body: {
          rows: [row(), row({ email: "bounces@example.com" })],
          ready: 2,
          total: 2,
        },
      },
      {
        match: "/bulk",
        status: 202,
        body: {
          rows: [
            row({ outcome: "sent", detail: "Invitation sent." }),
            row({
              email: "bounces@example.com",
              outcome: "failed",
              detail: "The invitation could not be emailed. Try this row again.",
            }),
          ],
          sent: 1,
          failed: 1,
        },
      },
      {
        match: "/bulk",
        status: 202,
        body: {
          rows: [row({ email: "bounces@example.com", outcome: "sent", detail: "Invitation sent." })],
          sent: 1,
          failed: 0,
        },
      },
    );
    renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));
    await screen.findByRole("table");
    await userEvent.click(screen.getByRole("button", { name: "Send 2 invitations" }));

    await userEvent.click(await screen.findByRole("button", { name: "Retry 1 row" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    // The retry carries the failure and nothing else — not the row already invited.
    expect(sentBody(fetchMock, 2)).toEqual({
      rows: [{ email: "bounces@example.com", role: "member", role_title: null }],
    });
  });

  it("does not invite anybody when the send is refused", async () => {
    routeFetch(
      { match: "/preview", status: 200, body: { rows: [row()], ready: 1, total: 1 } },
      {
        match: "/bulk",
        status: 403,
        body: envelope("permission_denied", "Your role does not include inviting people."),
      },
    );
    const { onInvited } = renderPanel();

    await userEvent.type(screen.getByLabelText("Work emails"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Check the list" }));
    await screen.findByRole("table");
    await userEvent.click(screen.getByRole("button", { name: "Send 1 invitation" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Your role does not include inviting people.",
    );
    expect(onInvited).not.toHaveBeenCalled();
  });
});

describe("the roles it offers", () => {
  it("offers only what the caller outranks", async () => {
    // A courtesy, not the enforcement: the server re-checks the ceiling per row on the
    // send, which is what makes an edited request no more powerful than this dropdown.
    renderPanel();

    const options = within(screen.getByLabelText("Role for everyone")).getAllByRole("option");
    expect(options.map((option) => option.getAttribute("value"))).toEqual([...GRANTABLE]);
  });
});
