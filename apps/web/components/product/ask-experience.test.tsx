import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AskExperience } from "@/components/product/ask-experience";
import {
  calledMethod,
  calledUrl,
  envelope,
  routeFetch,
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

  it("suggests questions about the asker's own work, never about someone else's handover", () => {
    scriptFetch();
    renderWithQuery(<AskExperience />);

    const group = screen.getByRole("group", { name: /suggested questions/i });
    expect(group).toHaveTextContent(/my|am I|have I/);
    expect(group).not.toHaveTextContent(/package|recipient|subject|knowledge transfer|KT/i);
  });
});

async function askSomething(question = "Tell me about my Astro Agent project") {
  await userEvent.type(screen.getByLabelText(/your question/i), question);
  await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
}

describe("refusals", () => {
  it("says nothing the asker may read is searchable yet, names the organisation, and points at where content comes from", async () => {
    routeFetch(
      {
        match: "/v1/ask",
        status: 200,
        body: answer({
          answer: null,
          insufficient_evidence: true,
          refusal_reason: "no_authorized_evidence",
          citations: [],
          sources: [],
          attempts: 0,
        }),
      },
      { match: "/v1/me/organisation", status: 200, body: { name: "Example Analytical" } },
    );
    renderWithQuery(<AskExperience />);

    await askSomething();

    // The visible refusal, not the live region that announces the same fact.
    expect(
      await screen.findByText(/could be searched yet, so there was no evidence to answer from/i),
    ).toBeInTheDocument();
    expect(await screen.findByText("Example Analytical")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /connect an application/i })).toHaveAttribute(
      "href",
      "/me/integrations",
    );
    expect(screen.getByRole("link", { name: /knowledge basket/i })).toHaveAttribute(
      "href",
      "/me/basket",
    );
    // Not the other refusal: nothing was retrieved, so rephrasing cannot help.
    expect(screen.queryByText(/does not answer this/i)).not.toBeInTheDocument();
  });

  it("keeps the evidence-does-not-answer refusal for evidence that was retrieved", async () => {
    scriptFetch({
      status: 200,
      body: answer({
        answer: null,
        insufficient_evidence: true,
        refusal_reason: "evidence_does_not_answer",
        citations: [],
      }),
    });
    renderWithQuery(<AskExperience />);

    await askSomething();

    expect(await screen.findByText(/does not answer this/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /connect an application/i })).not.toBeInTheDocument();
  });
});

describe("citations", () => {
  it("labels an extracted claim and opens the original only at the address the server sent", async () => {
    scriptFetch({
      status: 200,
      body: answer({
        answer: "Astro Agent is your project [1], described in its notes [2].",
        citations: [
          {
            marker: 1,
            chunk_id: "c1",
            document_id: "d1",
            document_title: "astro-agent README",
            source_system: "github",
            kind: "claim",
            claim_type: "project",
            occurred_at: "2026-09-01T10:00:00Z",
            source_uri: "https://github.com/example/astro-agent",
          },
          {
            marker: 2,
            chunk_id: "c2",
            document_id: "d2",
            document_title: "Planning notes",
            source_system: "basket",
            kind: "passage",
            source_uri: null,
          },
        ],
      }),
    });
    renderWithQuery(<AskExperience />);

    await askSomething();

    expect(await screen.findByText(/Project · github/)).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: /open original/i });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "https://github.com/example/astro-agent");
    expect(links[0]).toHaveAttribute("target", "_blank");
    expect(links[0]).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("never renders an address a browser would execute as a link", async () => {
    scriptFetch({
      status: 200,
      body: answer({
        answer: "Grounded [1].",
        citations: [
          {
            marker: 1,
            chunk_id: "c1",
            document_id: "d1",
            document_title: "Suspicious",
            source_system: "local",
            kind: "passage",
            source_uri: "javascript:alert(1)",
          },
        ],
      }),
    });
    renderWithQuery(<AskExperience />);

    await askSomething();

    expect(await screen.findByText(/Suspicious/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /open original/i })).not.toBeInTheDocument();
  });
});
