import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AskExperience } from "@/components/product/ask-experience";
import {
  calledMethod,
  calledUrl,
  envelope,
  scriptFetch,
  sentBody,
  type Json,
} from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";

/**
 * The cited-answer experience against a scripted API.
 *
 * What matters: the request carries only the question (the model is the server's,
 * §28), citations render exactly as returned and are never invented here (§27), an
 * insufficient_evidence response renders as an honest refusal, and an unconfigured
 * deployment degrades to retrieval with the reason on screen.
 */

function answer(overrides: Json = {}): Json {
  return {
    answer: "The platform stores rows in Postgres [1] and edges in Neo4j [2].",
    insufficient_evidence: false,
    citations: [
      {
        marker: 1,
        chunk_id: "c1",
        document_id: "d1",
        document_title: "Architecture decision record",
        source_system: "local",
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

describe("asking", () => {
  it("sends only the question — never a model, prompt or filter", async () => {
    const fetchMock = scriptFetch({ status: 200, body: answer() });
    renderWithQuery(<AskExperience />);

    await userEvent.type(screen.getByLabelText(/your question/i), "What stores data?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));

    await screen.findByText(/stores rows in Postgres/);
    expect(calledUrl(fetchMock, 0)).toBe("/api/jutsu/v1/ask");
    expect(calledMethod(fetchMock, 0)).toBe("POST");
    expect(Object.keys(sentBody(fetchMock, 0))).toEqual(["question"]);
  });

  it("renders the server's citations and nothing it made up itself", async () => {
    scriptFetch({ status: 200, body: answer() });
    renderWithQuery(<AskExperience />);

    await userEvent.type(screen.getByLabelText(/your question/i), "What stores data?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));

    expect(await screen.findByText("Architecture decision record", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Graph design doc", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("[1]")).toBeInTheDocument();
    expect(screen.getByText("[2]")).toBeInTheDocument();
  });

  it("renders insufficient evidence as a refusal, never an empty answer", async () => {
    scriptFetch({
      status: 200,
      body: answer({ answer: null, insufficient_evidence: true, citations: [] }),
    });
    renderWithQuery(<AskExperience />);

    await userEvent.type(screen.getByLabelText(/your question/i), "Unanswerable?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));

    expect(await screen.findByText(/refuses rather than guesses/i)).toBeInTheDocument();
  });

  it("degrades to retrieval when the deployment has no answer provider", async () => {
    scriptFetch({
      status: 503,
      body: envelope(
        "service_unavailable",
        "Answers are not configured for this deployment yet. Retrieval still works.",
      ),
    });
    renderWithQuery(<AskExperience />);

    await userEvent.type(screen.getByLabelText(/your question/i), "Anything?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));

    expect(
      await screen.findByText(/answer synthesis is not configured/i),
    ).toBeInTheDocument();
    // The retrieval surface takes over — a search box, not a dead Ask.
    expect(screen.getByRole("button", { name: /search/i })).toBeInTheDocument();
  });

  it("offers suggested questions before the first exchange", () => {
    scriptFetch();
    renderWithQuery(<AskExperience />);

    expect(
      screen.getByRole("group", { name: /suggested questions/i }),
    ).toBeInTheDocument();
  });
});
