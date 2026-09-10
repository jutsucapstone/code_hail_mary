import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  describeRecognitionError,
  joinDictation,
  MAX_LISTEN_MS,
  transcriptOf,
  useVoiceInput,
  type RecognitionResultList,
} from "@/lib/voice-input";
import { FakeRecognition, installRecognition } from "@/test-support/speech";

/**
 * Voice input's lifecycle, against a recogniser the test drives event by event.
 *
 * The properties that matter are the ones a person would notice as a betrayal: the
 * microphone staying open after they pressed stop, left the tab, or left the page;
 * words heard after a cancel reaching the box anyway; and a refusal being reported as
 * nothing at all.
 */

function box() {
  return { onStart: vi.fn<() => void>(), onText: vi.fn<(text: string) => void>() };
}

function withBox() {
  const target = box();
  const hook = renderHook(() => useVoiceInput());
  act(() => {
    hook.result.current.attach(target);
  });
  return { hook, target };
}

beforeEach(() => {
  FakeRecognition.reset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("where it is offered", () => {
  it("offers nothing in a browser with no recogniser", () => {
    const { hook } = withBox();

    expect(hook.result.current.supported).toBe(false);
    act(() => hook.result.current.start());
    expect(hook.result.current.status).toBe("idle");
  });

  it("does not listen with nowhere to put the words", () => {
    installRecognition();
    const hook = renderHook(() => useVoiceInput());

    act(() => hook.result.current.start());

    expect(FakeRecognition.instances).toHaveLength(0);
    expect(hook.result.current.attached).toBe(false);
  });
});

describe("a session", () => {
  it("listens once, in the person's own language, and writes interim words as they come", () => {
    installRecognition();
    const { hook, target } = withBox();

    act(() => hook.result.current.start());
    const recognizer = FakeRecognition.latest();

    expect(recognizer.lang).toBe(navigator.language);
    expect(recognizer.continuous).toBe(false);
    expect(recognizer.interimResults).toBe(true);
    expect(recognizer.start).toHaveBeenCalledTimes(1);
    expect(target.onStart).toHaveBeenCalledTimes(1);
    expect(hook.result.current.status).toBe("starting");

    act(() => recognizer.started());
    expect(hook.result.current.status).toBe("listening");

    act(() => recognizer.hears("what stores"));
    expect(target.onText).toHaveBeenLastCalledWith("what stores");

    act(() => recognizer.settles("what stores", " data"));
    expect(target.onText).toHaveBeenLastCalledWith("what stores data");

    act(() => recognizer.ends());
    expect(hook.result.current.status).toBe("idle");
  });

  it("ignores a second start while one session runs", () => {
    installRecognition();
    const { hook } = withBox();

    act(() => hook.result.current.start());
    act(() => hook.result.current.start());

    expect(FakeRecognition.instances).toHaveLength(1);
  });

  it("finishes on stop and keeps the words", () => {
    installRecognition();
    const { hook, target } = withBox();
    act(() => hook.result.current.start());
    const recognizer = FakeRecognition.latest();
    act(() => recognizer.started());

    act(() => hook.result.current.stop());
    expect(hook.result.current.status).toBe("stopping");
    expect(recognizer.stop).toHaveBeenCalledTimes(1);

    act(() => {
      recognizer.settles("who owns Falcon");
      recognizer.ends();
    });
    expect(target.onText).toHaveBeenLastCalledWith("who owns Falcon");
    expect(hook.result.current.status).toBe("idle");
  });

  it("tears down on cancel, and a word heard afterwards reaches nobody", () => {
    installRecognition();
    const { hook, target } = withBox();
    act(() => hook.result.current.start());
    const recognizer = FakeRecognition.latest();
    act(() => recognizer.hears("half a"));

    act(() => hook.result.current.cancel());
    expect(recognizer.abort).toHaveBeenCalledTimes(1);
    expect(hook.result.current.status).toBe("idle");

    act(() => recognizer.hears("half a sentence"));
    expect(target.onText).toHaveBeenCalledTimes(1);
  });

  it("reports a recogniser that refuses to start instead of hanging in 'starting'", () => {
    installRecognition();
    FakeRecognition.failNextStart = true;
    const { hook } = withBox();

    act(() => hook.result.current.start());

    expect(hook.result.current.status).toBe("idle");
    expect(hook.result.current.error?.code).toBe("failed");
  });
});

describe("refusals", () => {
  it("explains a blocked microphone, and clears it on the next attempt", () => {
    installRecognition();
    const { hook } = withBox();
    act(() => hook.result.current.start());
    const recognizer = FakeRecognition.latest();

    act(() => {
      recognizer.fails("not-allowed");
      recognizer.ends();
    });
    expect(hook.result.current.error?.code).toBe("blocked");
    expect(hook.result.current.error?.message).toMatch(/microphone access is blocked/i);
    expect(hook.result.current.status).toBe("idle");

    act(() => hook.result.current.start());
    expect(hook.result.current.error).toBeNull();
  });

  it("treats an abort as the cancel it is, not as an error", () => {
    installRecognition();
    const { hook } = withBox();
    act(() => hook.result.current.start());

    act(() => FakeRecognition.latest().fails("aborted"));

    expect(hook.result.current.error).toBeNull();
  });

  it("has a sentence for every documented error, and one for the rest", () => {
    const codes: Record<string, string> = {
      "not-allowed": "blocked",
      "service-not-allowed": "unavailable",
      "audio-capture": "no-microphone",
      "no-speech": "no-speech",
      network: "network",
      "language-not-supported": "language",
      "bad-grammar": "failed",
    };
    for (const [error, code] of Object.entries(codes)) {
      const described = describeRecognitionError(error, "en-IN");
      expect(described?.code, error).toBe(code);
      expect(described?.message.length, error).toBeGreaterThan(20);
    }
    expect(describeRecognitionError("language-not-supported", "en-IN")?.message).toContain("en-IN");
    expect(describeRecognitionError("aborted", "en-IN")).toBeNull();
  });
});

describe("a microphone never outlives the person's attention", () => {
  it("is told to finish at the ceiling, in a room too noisy to pause", () => {
    vi.useFakeTimers();
    installRecognition();
    const { hook } = withBox();
    act(() => hook.result.current.start());
    const recognizer = FakeRecognition.latest();

    act(() => {
      vi.advanceTimersByTime(MAX_LISTEN_MS);
    });

    expect(recognizer.stop).toHaveBeenCalledTimes(1);
    expect(hook.result.current.status).toBe("stopping");
  });

  it("ends when the tab is hidden", () => {
    installRecognition();
    const { hook } = withBox();
    act(() => hook.result.current.start());
    const recognizer = FakeRecognition.latest();

    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    visibility.mockRestore();

    expect(recognizer.abort).toHaveBeenCalledTimes(1);
    expect(hook.result.current.status).toBe("idle");
  });

  it("ends when the component goes", () => {
    installRecognition();
    const { hook } = withBox();
    act(() => hook.result.current.start());
    const recognizer = FakeRecognition.latest();

    hook.unmount();

    expect(recognizer.abort).toHaveBeenCalledTimes(1);
  });

  it("ends when the box it was writing into detaches", () => {
    installRecognition();
    const target = box();
    const hook = renderHook(() => useVoiceInput());
    let detach = () => {};
    act(() => {
      detach = hook.result.current.attach(target);
    });
    act(() => hook.result.current.start());
    const recognizer = FakeRecognition.latest();

    act(() => detach());

    expect(recognizer.abort).toHaveBeenCalledTimes(1);
    expect(hook.result.current.attached).toBe(false);
  });
});

describe("on-device recognition", () => {
  it("keeps the audio on the device when the browser has the model installed", async () => {
    installRecognition();
    const available = vi.fn().mockResolvedValue("available");
    FakeRecognition.available = available;
    const { hook } = withBox();

    await waitFor(() => expect(hook.result.current.onDevice).toBe(true));
    act(() => hook.result.current.start());

    expect(available).toHaveBeenCalledWith({ langs: [navigator.language], processLocally: true });
    expect(FakeRecognition.latest().processLocally).toBe(true);
  });

  it("uses the browser's service, and says so, when the model is not installed", async () => {
    installRecognition();
    const available = vi.fn().mockResolvedValue("downloadable");
    FakeRecognition.available = available;
    const { hook } = withBox();

    await waitFor(() => expect(available).toHaveBeenCalled());
    act(() => hook.result.current.start());

    expect(hook.result.current.onDevice).toBe(false);
    expect(FakeRecognition.latest().processLocally).toBeUndefined();
  });
});

describe("the words", () => {
  function list(...phrases: string[]): RecognitionResultList {
    return phrases.map((transcript) => [{ transcript }]) as unknown as RecognitionResultList;
  }

  it("joins every result and collapses the recogniser's stray spaces", () => {
    expect(transcriptOf(list(" what  stores", " data "))).toBe("what stores data");
    expect(transcriptOf(list())).toBe("");
  });

  it("appends speech to typing with one space, and never erases what was typed", () => {
    expect(joinDictation("About Falcon:", "who owns it")).toBe("About Falcon: who owns it");
    expect(joinDictation("About Falcon:  ", "who owns it")).toBe("About Falcon: who owns it");
    expect(joinDictation("", "who owns it")).toBe("who owns it");
    expect(joinDictation("typed", "")).toBe("typed");
  });
});
