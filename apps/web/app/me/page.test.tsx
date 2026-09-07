import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import MePage from "@/app/me/page";
import { MEMBER_SECTIONS } from "@/lib/member-nav";
import { capabilities, type Json } from "@/test-support/api";

/**
 * The employee home page, against a scripted principal.
 *
 * What this file pins is that the page's next step is *takeable*: the prose used to name
 * Integrations and offer no way to get there, and prose is not an affordance. So the
 * assertions are about links and their targets — and about the targets being sections
 * `MEMBER_SECTIONS` actually declares live, because a confident button onto a 404 is
 * worse than the sentence it replaced.
 *
 * No `fetch` here. Everything on this page comes from the capabilities the shell already
 * resolved, so the seam is the context, not the network.
 */

const shell = vi.hoisted(() => ({ capabilities: {} as Json }));

vi.mock("@/components/member/member-shell", () => ({
  useMemberCapabilities: () => shell.capabilities,
}));

beforeEach(() => {
  vi.clearAllMocks();
  shell.capabilities = capabilities({ role: "member" });
});

/** The nav entry that owns a destination, so a test cannot drift from the IA. */
function section(name: string) {
  const found = MEMBER_SECTIONS.find((item) => item.name === name);
  if (!found) throw new Error(`no member section named "${name}"`);
  return found;
}

describe("what happens next", () => {
  it("offers a way to connect a tool and a way to ask a question", () => {
    render(<MePage />);

    expect(screen.getByRole("link", { name: /connect a tool/i })).toHaveAttribute(
      "href",
      "/me/integrations",
    );
    expect(screen.getByRole("link", { name: /ask a question/i })).toHaveAttribute("href", "/ask");
  });

  it("points only at sections the console actually serves", () => {
    render(<MePage />);

    const integrations = section("My integrations");
    const ask = section("Ask JUTSU");
    expect(integrations.status).toBe("live");
    expect(ask.status).toBe("live");

    expect(screen.getByRole("link", { name: /connect a tool/i })).toHaveAttribute(
      "href",
      integrations.href,
    );
    expect(screen.getByRole("link", { name: /ask a question/i })).toHaveAttribute("href", ask.href);
  });

  it("withholds asking from a caller who does not hold retrieval:query", () => {
    shell.capabilities = capabilities({ role: "member", permissions: ["profile:self_read"] });
    render(<MePage />);

    // The door onto a 403 is the one that must not be drawn; connecting is open to
    // everyone, so it stays.
    expect(screen.queryByRole("link", { name: /ask a question/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /connect a tool/i })).toBeInTheDocument();
  });
});

describe("identity", () => {
  it("still confirms the JUTSU ID and the role it was issued under", () => {
    render(<MePage />);

    expect(screen.getByText("JUTSU-ADM-9HXPNFG8")).toBeInTheDocument();
    expect(screen.getByText("Member")).toBeInTheDocument();
  });
});
