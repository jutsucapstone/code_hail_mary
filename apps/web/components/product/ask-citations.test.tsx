import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AskExperience } from "@/components/product/ask-experience";
import {
  calledMethod,
  calledUrl,
  callIndexFor,
  envelope,
  routeFetch,
  type Json,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The citations on `/ask`, as controls rather than decoration.
 *
 * §3 surface 1 and §24 promise an answer whose citations reach a source span, and the
 * failure mode this file exists to catch is the quiet one: markers that render, look
 * cited, and do nothing. So each test presses something and asserts what the browser
 * asked for — a span fetched once per chunk and only when a reader asks, the returned
 * text rendered whole, and a failed or no-longer-readable chunk leaving the answer
 * standing rather than blanking it.
 *
 * Separate from `ask-experience.test.tsx`, which is about the request and the response
 * shape. This one is about what a reader can do with the answer once it is there.
 */

const ANSWER = "Postgres holds the rows [1] and Neo4j holds the edges [2].";

function answer(overrides: Json = {}): Json {
  return {
    answer: ANSWER,
    insufficient_evidence: false,
    citations: [
      {
        marker: 1,
        chunk_id: "c1",
        document_id: "d1",
        document_title: "Architecture decision record",
        source_system: "gmail",
      },
      {
        marker: 2,
        chunk_id: "c2",
        document_id: "d2",
        document_title: "Graph design doc",
        source_system: "local",
      },
    ],
    sources: [],
    attempts: 1,
    query_tokens: 9,
    ...overrides,
  };
}

/** What `/v1/evidence/{chunk_id}` returns: the masked text and the original's offsets. */
function span(overrides: Json = {}): Json {
  return {
    chunk_id: "c1",
    document_id: "d1",
    document_title: "Architecture decision record",
    source_system: "gmail",
    text: "We chose Postgres for rows and Neo4j for edges, and told [EMAIL_A7] so.",
    char_start: 986,
    char_end: 1204,
    occurred_at: "2001-05-17T00:00:00Z",
    ...overrides,
  };
}

type User = ReturnType<typeof userEvent.setup>;

async function askQuestion(user: User) {
  await user.type(screen.getByLabelText(/your question/i), "What holds what?");
  await user.click(screen.getByRole("button", { name: /^ask$/i }));
}

/** How many times the browser reached the evidence endpoint, whatever the chunk. */
function evidenceCalls(fetchMock: ReturnType<typeof routeFetch>): number {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes("/v1/evidence/"))
    .length;
}

describe("citations", () => {
  it("opens the cited span from the marker, and asks for it once however often it is pressed", async () => {
    const user = userEvent.setup();
    const fetchMock = routeFetch(
      { match: "/v1/ask", status: 200, body: answer() },
      { match: "/v1/evidence/c1", status: 200, body: span() },
    );
    renderWithQuery(<AskExperience />);

    await askQuestion(user);
    await screen.findByText(/Postgres holds the rows/);

    const marker = screen.getByRole("button", {
      name: /show source 1: architecture decision record/i,
    });
    // Nothing is fetched until a reader asks — an answer citing eight documents must not
    // cost eight requests for the one span anybody opens.
    expect(evidenceCalls(fetchMock)).toBe(0);

    await user.click(marker);
    expect(await screen.findByText(/We chose Postgres for rows/)).toBeInTheDocument();

    await user.click(marker);
    expect(evidenceCalls(fetchMock)).toBe(1);
    // Still open, and still the fetched span: a second press reveals, it does not toggle
    // away what the reader just asked to see.
    expect(screen.getByText(/We chose Postgres for rows/)).toBeInTheDocument();

    const index = callIndexFor(fetchMock, "/v1/evidence/");
    expect(calledUrl(fetchMock, index)).toBe("/api/jutsu/v1/evidence/c1");
    expect(calledMethod(fetchMock, index)).toBe("GET");
  });

  it("renders the span whole, with its source system and character range as numbers", async () => {
    const user = userEvent.setup();
    routeFetch(
      { match: "/v1/ask", status: 200, body: answer() },
      { match: "/v1/evidence/c1", status: 200, body: span() },
    );
    renderWithQuery(<AskExperience />);

    await askQuestion(user);
    await screen.findByText(/Postgres holds the rows/);
    await user.click(screen.getByRole("button", { name: /show source 1/i }));

    // The whole returned text, never a slice of it: `char_start`/`char_end` index the
    // original document while `text` is masked, so highlighting with them would land
    // somewhere else. The offsets are shown as the numbers they are instead.
    expect(await screen.findByText(span().text as string)).toBeInTheDocument();
    expect(screen.getByText(/gmail · chars 986–1204/)).toBeInTheDocument();
  });

  it("opens the same span from the sources list", async () => {
    const user = userEvent.setup();
    const fetchMock = routeFetch(
      { match: "/v1/ask", status: 200, body: answer() },
      { match: "/v1/evidence/c2", status: 200, body: span({ chunk_id: "c2", text: "The edge model." }) },
    );
    renderWithQuery(<AskExperience />);

    await askQuestion(user);
    await screen.findByText(/Postgres holds the rows/);
    await user.click(screen.getByRole("button", { name: /\[2\] graph design doc/i }));

    expect(await screen.findByText("The edge model.")).toBeInTheDocument();
    expect(calledUrl(fetchMock, callIndexFor(fetchMock, "/v1/evidence/"))).toBe(
      "/api/jutsu/v1/evidence/c2",
    );
  });

  it("reports a chunk it can no longer read without blanking the answer", async () => {
    const user = userEvent.setup();
    routeFetch(
      { match: "/v1/ask", status: 200, body: answer() },
      // 404, not 403: the endpoint answers the same way for never-existed and
      // not-granted-to-you, so this is what a revoked or superseded source looks like.
      { match: "/v1/evidence/c1", status: 404, body: envelope("not_found", "Not found.") },
    );
    renderWithQuery(<AskExperience />);

    await askQuestion(user);
    await screen.findByText(/Postgres holds the rows/);
    await user.click(screen.getByRole("button", { name: /show source 1/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/not available to you any more/i);
    // The row says what happened rather than "Source shown", which would be a lie about
    // a panel holding nothing but the refusal.
    expect(screen.getByText("Source unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Postgres holds the rows/)).toBeInTheDocument();
    expect(screen.getByText("[2]")).toBeInTheDocument();
  });

  it("announces that the answer arrived and how many sources it cites", async () => {
    const user = userEvent.setup();
    routeFetch({ match: "/v1/ask", status: 200, body: answer() });
    renderWithQuery(<AskExperience />);

    await askQuestion(user);
    await screen.findByText(/Postgres holds the rows/);

    // `LoadingRegion` announces the start of the wait; without this a screen-reader user
    // hears "Composing an answer" and then silence over a screenful of new text.
    expect(screen.getByRole("status")).toHaveTextContent(/answer ready, citing 2 sources/i);
  });

  it("leaves a marker the server did not cite as prose", async () => {
    const user = userEvent.setup();
    routeFetch({
      match: "/v1/ask",
      status: 200,
      body: answer({ answer: "Rows live in Postgres [1], and a stray marker [3]." }),
    });
    renderWithQuery(<AskExperience />);

    await askQuestion(user);
    await screen.findByText(/stray marker \[3\]/);

    // The citation set was validated server-side (§27). A button for a number this page
    // cannot resolve would be the frontend minting a citation.
    expect(screen.queryByRole("button", { name: /show source 3/i })).toBeNull();
    expect(screen.getByRole("button", { name: /show source 1/i })).toBeInTheDocument();
  });
});
