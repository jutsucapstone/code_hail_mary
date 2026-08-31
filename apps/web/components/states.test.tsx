import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  EmptyState,
  ErrorState,
  FailureState,
  NotBuiltYet,
  PermissionDenied,
  ReauthRequired,
} from "@/components/states";
import { ApiError } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";

function failureFor(status: number, message = "Refused.") {
  return classifyApiError(
    new ApiError(status, {
      error: { code: "x", message, details: {} },
      request_id: "req-7",
    }),
  );
}

describe("the empty state", () => {
  it("says the request succeeded and there is nothing to show", () => {
    // The state most often skipped, and the one whose absence costs most: a blank panel
    // is indistinguishable from a failed request, so a reader goes hunting for a bug that
    // is not there.
    render(
      <EmptyState title="No employees yet">
        <p>Invite someone to get started.</p>
      </EmptyState>,
    );

    expect(screen.getByRole("heading", { name: "No employees yet" })).toBeInTheDocument();
    expect(screen.getByText("Invite someone to get started.")).toBeInTheDocument();
  });

  it("is not an alert — nothing went wrong", () => {
    render(
      <EmptyState title="Nothing here">
        <p>Still empty.</p>
      </EmptyState>,
    );

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("permission denied", () => {
  it("does not send the reader to a screen that does not exist", () => {
    // It used to end "…can change that from Roles & permissions". That section is
    // `pending` in admin-nav.ts and `member:assign_role` is declared by no route, so the
    // sentence sent people looking for a door that is not there.
    render(<PermissionDenied what="reading the audit log" />);

    expect(screen.getByText(/reading the audit log/)).toBeInTheDocument();
    expect(screen.queryByText(/Roles & permissions/i)).not.toBeInTheDocument();
  });
});

describe("re-authentication", () => {
  it("is distinct from a permission problem", () => {
    // Telling someone whose session merely expired to ask an administrator for more
    // permissions sends them to a colleague for nothing.
    render(<ReauthRequired />);

    expect(screen.getByRole("heading", { name: /session has expired/i })).toBeInTheDocument();
    expect(screen.queryByText(/does not include/i)).not.toBeInTheDocument();
  });

  it("offers to sign in again when the caller can act on it", async () => {
    const onSignIn = vi.fn();
    render(<ReauthRequired onSignIn={onSignIn} />);

    await userEvent.click(screen.getByRole("button", { name: /sign in again/i }));
    expect(onSignIn).toHaveBeenCalledOnce();
  });
});

describe("the error state", () => {
  it("shows the request id, which identifies a request and not a person", () => {
    render(<ErrorState message="It broke." requestId="req-42" />);
    expect(screen.getByText(/req-42/)).toBeInTheDocument();
  });

  it("hides the placeholder id used when the API never answered", () => {
    // "unknown" is what lib/api.ts substitutes for a response with no envelope. Printing
    // "Reference unknown" invites somebody to quote it to support.
    render(<ErrorState message="It broke." requestId="unknown" />);
    expect(screen.queryByText(/unknown/i)).not.toBeInTheDocument();
  });
});

describe("FailureState routes a classified failure to the right screen", () => {
  it("renders a 403 as a denial, not as 'that did not load'", () => {
    render(<FailureState failure={failureFor(403)} deniedWhat="the audit log" />);

    expect(screen.getByRole("heading", { name: /do not have access/i })).toBeInTheDocument();
    expect(screen.queryByText(/did not load/i)).not.toBeInTheDocument();
  });

  it("renders a 401 as re-authentication", () => {
    render(<FailureState failure={failureFor(401)} />);
    expect(screen.getByRole("heading", { name: /session has expired/i })).toBeInTheDocument();
  });

  it("withholds the retry button on a failure that cannot change", () => {
    const onRetry = vi.fn();
    render(<FailureState failure={failureFor(422, "Too long.")} onRetry={onRetry} />);

    expect(screen.getByText("Too long.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
  });

  it("offers the retry button where a second attempt could work", async () => {
    const onRetry = vi.fn();
    render(<FailureState failure={failureFor(503, "Provider down.")} onRetry={onRetry} />);

    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});

describe("not built yet", () => {
  it("names the slice on screen rather than in a tooltip", () => {
    render(<NotBuiltYet name="Audit log" slice="P2" />);

    expect(screen.getByText("P2")).toBeInTheDocument();
    // §4.11: it must say it is empty, not stand in for data.
    expect(screen.getByText(/nothing is behind it/i)).toBeInTheDocument();
  });
});
