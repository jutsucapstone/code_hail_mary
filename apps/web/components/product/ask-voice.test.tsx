import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AskWorkspace } from "@/components/product/ask-workspace";
import { envelope, scriptFetch, sentBody, type Json } from "@/test-support/api";
import { renderWithQuery } from "@/test-support/render";
import { FakeRecognition, installRecognition } from "@/test-support/speech";

/**
 * Asking by voice, end to end through the question box.
 *
 * The contract: speaking only ever FILLS the box. Nothing is sent until the person
 * presses Ask, what they typed first is kept, Escape puts the box back, and the
 * question that leaves is exactly the one on screen — through the same request a
 * typed question makes. The orb is replaced by a marker: jsdom has no WebGL, and what
 * matters here is only when it is shown and whether it is told to listen.
 */

vi.mock("@/components/ui/voice-powered-orb", () => ({
  VoicePoweredOrb: ({ enableVoiceControl }: { enableVoiceControl?: boolean }) => (
    <div data-testid="voice-orb" data-listening={String(Boolean(enableVoiceControl))} />
  ),
}));

function answer(): Json {
  return {
    answer: "Rows live in Postgres [1].",
    insufficient_evidence: false,
    citations: [
      {
        marker: 1,
        chunk_id: "c1",
        document_id: "d1",
        document_title: "Architecture decision record",
        source_system: "local",
      },
    ],
    sources: [],
    attempts: 1,
    query_tokens: 9,
  };
}

function renderPage() {
  return renderWithQuery(
    <AskWorkspace>
      <h1>Cited Q&amp;A</h1>
    </AskWorkspace>,
  );
}

const questionBox = () => screen.getByLabelText(/your question/i);

async function startSpeaking() {
  await userEvent.click(await screen.findByRole("button", { name: /ask by voice/i }));
  const recognizer = FakeRecognition.latest();
  act(() => recognizer.started());
  return recognizer;
}

beforeEach(() => {
  FakeRecognition.reset();
});

describe("where voice is offered", () => {
  it("is absent, and the box is exactly the typed one, in a browser with no recogniser", () => {
    scriptFetch();
    renderPage();

    expect(screen.queryByRole("button", { name: /ask by voice/i })).toBeNull();
    expect(questionBox()).not.toHaveAttribute("readonly");
  });

  it("goes away when the page falls back to evidence search", async () => {
    installRecognition();
    scriptFetch({
      status: 503,
      body: envelope("service_unavailable", "Answers are not configured for this deployment yet."),
    });
    renderPage();

    await userEvent.type(questionBox(), "Anything?");
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));

    expect(await screen.findByText(/answer synthesis is not configured/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /ask by voice/i })).toBeNull();
  });
});

describe("speaking a question", () => {
  it("fills the box as the words come, and sends nothing until Ask is pressed", async () => {
    installRecognition();
    const fetchMock = scriptFetch({ status: 200, body: answer() });
    renderPage();

    const recognizer = await startSpeaking();
    act(() => recognizer.hears("what stores"));
    expect(questionBox()).toHaveValue("what stores");
    expect(questionBox()).toHaveAttribute("readonly");
    expect(screen.getByText(/listening — ask your question/i)).toBeInTheDocument();

    act(() => {
      recognizer.settles("what stores data");
      recognizer.ends();
    });
    expect(questionBox()).toHaveValue("what stores data");
    expect(questionBox()).not.toHaveAttribute("readonly");
    expect(fetchMock).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));

    await screen.findByText(/rows live in postgres/i);
    expect(sentBody(fetchMock, 0)).toEqual({ question: "what stores data" });
  });

  it("keeps what was typed first and appends what was said", async () => {
    installRecognition();
    scriptFetch();
    renderPage();

    await userEvent.type(questionBox(), "About Falcon:");
    const recognizer = await startSpeaking();
    act(() => recognizer.hears("who owns it"));

    expect(questionBox()).toHaveValue("About Falcon: who owns it");
  });

  it("puts the box back the way it was on Escape", async () => {
    installRecognition();
    scriptFetch();
    renderPage();

    await userEvent.type(questionBox(), "my draft");
    const recognizer = await startSpeaking();
    act(() => recognizer.hears("something I did not mean"));
    await userEvent.keyboard("{Escape}");

    expect(questionBox()).toHaveValue("my draft");
    expect(recognizer.abort).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /ask by voice/i })).toBeInTheDocument();
  });

  it("sends what was heard when Ask is pressed mid-sentence, and stops listening", async () => {
    installRecognition();
    const fetchMock = scriptFetch({ status: 200, body: answer() });
    renderPage();

    const recognizer = await startSpeaking();
    act(() => recognizer.hears("what stores data"));
    await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));

    expect(recognizer.abort).toHaveBeenCalledTimes(1);
    await screen.findByText(/rows live in postgres/i);
    expect(sentBody(fetchMock, 0)).toEqual({ question: "what stores data" });

    // A word the recogniser was still holding must not refill the cleared box.
    act(() => recognizer.hears("what stores data and more"));
    expect(questionBox()).toHaveValue("");
  });
});

describe("telling the person what is happening", () => {
  it("explains a blocked microphone in words, and offers voice again", async () => {
    installRecognition();
    scriptFetch();
    renderPage();

    const recognizer = await startSpeaking();
    act(() => {
      recognizer.fails("not-allowed");
      recognizer.ends();
    });

    expect(screen.getByText(/microphone access is blocked for this site/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ask by voice/i })).toBeInTheDocument();
  });

  it("shows the small orb only while listening on a screen with no orb column", async () => {
    installRecognition();
    scriptFetch();
    renderPage();

    expect(screen.queryByTestId("voice-orb")).toBeNull();
    const recognizer = await startSpeaking();
    expect(screen.getByTestId("voice-orb")).toHaveAttribute("data-listening", "true");

    act(() => recognizer.ends());
    expect(screen.queryByTestId("voice-orb")).toBeNull();
  });
});
