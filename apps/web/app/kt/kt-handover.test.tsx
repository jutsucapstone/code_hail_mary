import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import HandoverPage from "@/app/kt/[code]/handover/page";
import { KtShell } from "@/components/kt/kt-shell";
import {
  calledMethod,
  calledUrl,
  callIndexFor,
  envelope,
  routeFetch,
  type Json,
  type RoutedResponse,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * Compose summary, against a scripted API.
 *
 * What the server proves — whose knowledge the report holds, that it is grounded, that
 * nothing else leaks into it — is proven against Postgres in `test_kt_subject_scope.py`.
 * What this file pins is the browser's half: one press sends one POST carrying nothing
 * to print, the button cannot be pressed twice while a paid composition runs, success
 * yields the summary AND a real PDF to open or keep, every failure renders the API's own
 * sentence, and a stale PDF is released rather than held in memory for the life of the tab.
 */

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/kt/KT-JUTSU-AAAA0001/handover",
  useSearchParams: () => new URLSearchParams(),
}));

const CODE = "KT-JUTSU-AAAA0001";
const REPORT_URL = `/api/jutsu/v1/kt/${CODE}/handover-report`;
const PDF_BASE64 = btoa("%PDF-1.4\n% a shaped stand-in for the real bytes\n%%EOF\n");

function recipientPackage(): Json {
  return {
    kt_code: CODE,
    status: "claimed",
    scope: ["documents", "profile", "projects"],
    period_start: null,
    period_end: null,
    expires_at: "2026-10-01T00:00:00Z",
    created_at: "2026-09-01T00:00:00Z",
    subject: { display_name: "Grace Hopper", designation: "Staff Engineer", department: "Platform" },
  };
}

function report(overrides: Json = {}): Json {
  return {
    summary: "Grace owned the Atlas migration [1].",
    insufficient_evidence: false,
    references: [
      { number: 1, document_title: "Atlas migration plan", source_system: "gmail", date: "2026-09-01" },
    ],
    attempts: 1,
    filename: "jutsu-handover-summary-20260914.pdf",
    pdf_base64: PDF_BASE64,
    generated_at: "2026-09-14T09:30:00Z",
    ...overrides,
  };
}

const createObjectURL = vi.fn();
const revokeObjectURL = vi.fn();

beforeEach(() => {
  createObjectURL.mockReset();
  revokeObjectURL.mockReset();
  createObjectURL.mockReturnValueOnce("blob:report-1").mockReturnValueOnce("blob:report-2");
  // jsdom implements neither; a browser does. Installed per test so a leak cannot carry.
  Object.defineProperty(URL, "createObjectURL", { value: createObjectURL, configurable: true, writable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: revokeObjectURL, configurable: true, writable: true });
});

function mount(...reports: Omit<RoutedResponse, "match">[]) {
  const fetchMock = routeFetch(
    { match: "/v1/kt/claim", status: 200, body: recipientPackage() },
    { match: "/insights-summary", status: 200, body: { by_type: { project: 1 } } },
    ...reports.map((response) => ({ match: "/handover-report", ...response })),
  );
  renderWithQuery(
    <KtShell code={CODE}>
      <HandoverPage />
    </KtShell>,
  );
  return fetchMock;
}

function reportCalls(fetchMock: ReturnType<typeof routeFetch>): number {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes("/handover-report")).length;
}

describe("Compose summary", () => {
  it("names the subject and composes nothing until pressed", async () => {
    const fetchMock = mount({ status: 200, body: report() });

    expect(await screen.findByRole("button", { name: /compose summary/i })).toBeEnabled();
    expect(screen.getAllByText(/Grace Hopper/).length).toBeGreaterThan(0);
    // Composing spends a budget and a model call; a page load must not.
    expect(reportCalls(fetchMock)).toBe(0);
  });

  it("sends one POST with nothing to print, then shows the summary, its sources and the PDF", async () => {
    const fetchMock = mount({ status: 200, body: report() });

    await userEvent.click(await screen.findByRole("button", { name: /compose summary/i }));

    expect(await screen.findByText("Grace owned the Atlas migration [1].")).toBeInTheDocument();
    expect(screen.getByText("[1] Atlas migration plan (gmail)")).toBeInTheDocument();

    const post = callIndexFor(fetchMock, "/handover-report");
    expect(calledUrl(fetchMock, post)).toBe(REPORT_URL);
    expect(calledMethod(fetchMock, post)).toBe("POST");
    // The server composes from the package. A browser that could send the text would
    // make the PDF a forgery kit with JUTSU's name on it.
    const init = (fetchMock.mock.calls[post] as unknown[])[1] as RequestInit | undefined;
    expect(init?.body).toBeUndefined();

    const download = screen.getByRole("link", { name: /download pdf/i });
    expect(download).toHaveAttribute("href", "blob:report-1");
    expect(download).toHaveAttribute("download", "jutsu-handover-summary-20260914.pdf");
    const open = screen.getByRole("link", { name: /open pdf/i });
    expect(open).toHaveAttribute("href", "blob:report-1");
    expect(open).toHaveAttribute("target", "_blank");
    expect(open.getAttribute("rel")).toContain("noopener");

    const pdf = createObjectURL.mock.calls[0][0] as Blob;
    expect(pdf.type).toBe("application/pdf");
    expect(pdf.size).toBe(atob(PDF_BASE64).length);
  });

  it("is busy and disabled while composing, so a second press charges nothing", async () => {
    const fetchMock = mount({ status: 200, body: null, pending: true });

    await userEvent.click(await screen.findByRole("button", { name: /compose summary/i }));

    const busy = await screen.findByRole("button", { name: /composing/i });
    expect(busy).toBeDisabled();
    expect(screen.getByRole("region", { name: "Executive summary" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText(/preparing the PDF/i)).toBeInTheDocument();

    await userEvent.click(busy);
    expect(reportCalls(fetchMock)).toBe(1);
  });

  it("offers the PDF even when no summary could be grounded, and says why", async () => {
    mount({ status: 200, body: report({ summary: null, insufficient_evidence: true, references: [] }) });

    await userEvent.click(await screen.findByRole("button", { name: /compose summary/i }));

    expect(await screen.findByText(/not enough to ground a summary yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/Grace owned/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /download pdf/i })).toHaveAttribute("href", "blob:report-1");
  });

  it("renders the API's own sentence when answers are unavailable, and stays usable", async () => {
    mount({
      status: 503,
      body: envelope(
        "service_unavailable",
        "Handover summaries are not configured for this deployment yet.",
      ),
    });

    await userEvent.click(await screen.findByRole("button", { name: /compose summary/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Handover summaries are not configured for this deployment yet.",
    );
    expect(screen.getByRole("button", { name: /compose summary/i })).toBeEnabled();
    expect(screen.queryByRole("link", { name: /download pdf/i })).not.toBeInTheDocument();
  });

  it("renders the package's own refusal when it closed in the meantime", async () => {
    mount({
      status: 403,
      body: envelope("permission_denied", "This Knowledge Transfer package has been revoked."),
    });

    await userEvent.click(await screen.findByRole("button", { name: /compose summary/i }));

    expect(
      await screen.findByText("This Knowledge Transfer package has been revoked."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /download pdf/i })).not.toBeInTheDocument();
  });

  it("releases the previous PDF when composing again", async () => {
    mount(
      { status: 200, body: report() },
      { status: 200, body: report({ filename: "jutsu-handover-summary-20260915.pdf" }) },
    );

    await userEvent.click(await screen.findByRole("button", { name: /compose summary/i }));
    expect(await screen.findByRole("link", { name: /download pdf/i })).toHaveAttribute(
      "href",
      "blob:report-1",
    );

    await userEvent.click(screen.getByRole("button", { name: /compose again/i }));

    await waitFor(() =>
      expect(screen.getByRole("link", { name: /download pdf/i })).toHaveAttribute(
        "href",
        "blob:report-2",
      ),
    );
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:report-1");
  });
});
