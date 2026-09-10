import { vi } from "vitest";

import type { Recognition, RecognitionResultList } from "@/lib/voice-input";

/**
 * A stand-in for the browser's speech recogniser.
 *
 * jsdom has no Web Speech API. This records how the recogniser was configured and lets
 * a test play the browser's half of the conversation — started, words heard, an error,
 * the end — one event at a time. The behaviour worth pinning is almost all about ORDER
 * (a word arriving after a cancel, an end arriving after an error), and a real
 * recogniser cannot be driven that precisely.
 */
export class FakeRecognition implements Recognition {
  static instances: FakeRecognition[] = [];
  static failNextStart = false;
  static available:
    | ((options: { langs: string[]; processLocally: boolean }) => Promise<string>)
    | undefined;

  lang = "";
  continuous = true;
  interimResults = false;
  maxAlternatives = 0;
  processLocally?: boolean;
  onstart: (() => void) | null = null;
  onresult: ((event: { readonly results: RecognitionResultList }) => void) | null = null;
  onerror: ((event: { readonly error: string }) => void) | null = null;
  onend: (() => void) | null = null;

  readonly start = vi.fn(() => {
    if (FakeRecognition.failNextStart) {
      FakeRecognition.failNextStart = false;
      throw new DOMException("recognition has already started", "InvalidStateError");
    }
  });
  readonly stop = vi.fn();
  readonly abort = vi.fn();

  constructor() {
    FakeRecognition.instances.push(this);
  }

  static reset() {
    FakeRecognition.instances = [];
    FakeRecognition.failNextStart = false;
    FakeRecognition.available = undefined;
  }

  static latest(): FakeRecognition {
    const latest = FakeRecognition.instances.at(-1);
    if (!latest) throw new Error("no recogniser was constructed");
    return latest;
  }

  started() {
    this.onstart?.();
  }

  /** Interim words: what a recogniser reports while the person is still talking. */
  hears(...phrases: string[]) {
    this.onresult?.({ results: resultList(phrases, false) });
  }

  /** Final words, after the pause. */
  settles(...phrases: string[]) {
    this.onresult?.({ results: resultList(phrases, true) });
  }

  fails(error: string) {
    this.onerror?.({ error });
  }

  ends() {
    this.onend?.();
  }
}

function resultList(phrases: string[], isFinal: boolean): RecognitionResultList {
  return phrases.map((transcript) =>
    Object.assign([{ transcript }], { isFinal }),
  ) as unknown as RecognitionResultList;
}

/** Put the fake where Chrome puts its recogniser. `vi.unstubAllGlobals` takes it away. */
export function installRecognition() {
  FakeRecognition.reset();
  vi.stubGlobal("webkitSpeechRecognition", FakeRecognition);
}
