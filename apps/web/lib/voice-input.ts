import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

/**
 * Voice input for the question box: speech in, text in the input, and nothing else.
 *
 * It is the browser's own recogniser (the Web Speech API) with no service of ours
 * behind it. That keeps the feature entirely in the frontend and keeps a spoken
 * question on exactly the path a typed one takes: the transcript is written into the
 * input, the person reads it, and Ask sends it through `POST /v1/ask` like anything
 * typed. **Nothing here submits.** A misheard question costs a correction, not a
 * spent answer budget and a confident reply to something nobody asked.
 *
 * **Where the audio goes is the browser's decision, and the page says which.** Chrome
 * sends it to its own speech service unless it can recognise on the device. When
 * `SpeechRecognition.available()` reports an installed on-device model for the
 * language, `processLocally` keeps the audio on the machine, and the status line tells
 * the person which of the two is happening while they speak. The transcript is never
 * logged or stored and goes nowhere but the input (§4.9).
 *
 * Browsers without the API (Firefox, today) get no microphone control at all rather
 * than one that fails when pressed.
 */

/**
 * The slice of the Web Speech API this uses. The recognition half is not in lib.dom —
 * only Chromium and WebKit ship it, one of them prefixed — so the shape is declared
 * here rather than assumed.
 */
export interface RecognitionAlternative {
  readonly transcript: string;
}

export interface RecognitionResult {
  readonly isFinal: boolean;
  readonly length: number;
  readonly [index: number]: RecognitionAlternative | undefined;
}

export interface RecognitionResultList {
  readonly length: number;
  readonly [index: number]: RecognitionResult | undefined;
}

export interface Recognition {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  /** Chrome's on-device switch. Absent elsewhere; setting it there is harmless. */
  processLocally?: boolean;
  onstart: (() => void) | null;
  onresult: ((event: { readonly results: RecognitionResultList }) => void) | null;
  onerror: ((event: { readonly error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

export interface RecognitionConstructor {
  new (): Recognition;
  available?(options: { langs: string[]; processLocally: boolean }): Promise<string>;
}

type SpeechWindow = Window & {
  SpeechRecognition?: RecognitionConstructor;
  webkitSpeechRecognition?: RecognitionConstructor;
};

/** Looked up when needed, never cached: what the page has now is what it can use. */
export function recognitionConstructor(): RecognitionConstructor | null {
  if (typeof window === "undefined") return null;
  const speech = window as SpeechWindow;
  return speech.SpeechRecognition ?? speech.webkitSpeechRecognition ?? null;
}

/** The person's own language, which is what they will speak; `en-US` as a last resort. */
export function speechLanguage(): string {
  if (typeof navigator !== "undefined" && navigator.language) return navigator.language;
  return "en-US";
}

/** Everything heard in the session so far, best alternative of each result, one line. */
export function transcriptOf(results: RecognitionResultList): string {
  const parts: string[] = [];
  for (let index = 0; index < results.length; index += 1) {
    const best = results[index]?.[0];
    if (best) parts.push(best.transcript);
  }
  return parts.join(" ").replace(/\s+/g, " ").trim();
}

/** What was typed before speaking, then what was said — never one over the other. */
export function joinDictation(typed: string, spoken: string): string {
  if (!spoken) return typed;
  const head = typed.trimEnd();
  return head ? `${head} ${spoken}` : spoken;
}

export type VoiceErrorCode =
  | "blocked"
  | "unavailable"
  | "no-microphone"
  | "no-speech"
  | "network"
  | "language"
  | "failed";

export interface VoiceError {
  code: VoiceErrorCode;
  message: string;
}

/**
 * A recogniser's error, in a sentence somebody can act on. `null` means "not an
 * error": `aborted` is what cancelling looks like from the inside.
 */
export function describeRecognitionError(error: string, lang: string): VoiceError | null {
  switch (error) {
    case "aborted":
      return null;
    case "not-allowed":
      return {
        code: "blocked",
        message:
          "Microphone access is blocked for this site. Allow it in your browser's site settings, then try again.",
      };
    case "service-not-allowed":
      return {
        code: "unavailable",
        message: "Voice input is turned off in this browser. Type your question instead.",
      };
    case "audio-capture":
      return {
        code: "no-microphone",
        message: "No microphone was found, or another app is using it.",
      };
    case "no-speech":
      return { code: "no-speech", message: "Nothing was heard. Try again when you're ready to speak." };
    case "network":
      return {
        code: "network",
        message:
          "Your browser's speech service could not be reached. Check your connection, or type your question.",
      };
    case "language-not-supported":
      return {
        code: "language",
        message: `Voice input does not support ${lang} in this browser. Type your question instead.`,
      };
    default:
      return {
        code: "failed",
        message: "Voice input stopped unexpectedly. Try again, or type your question.",
      };
  }
}

/**
 * A single utterance ends at the speaker's pause, but a noisy room can keep a
 * recogniser listening; this is the ceiling after which it is told to finish.
 */
export const MAX_LISTEN_MS = 30_000;

export type VoiceStatus = "idle" | "starting" | "listening" | "stopping";

/**
 * Where a transcript is written. The question box registers itself; only one target
 * exists at a time, and with none attached `start()` does nothing — there would be
 * nowhere for the words to go.
 */
export interface VoiceTarget {
  /** A session is beginning: snapshot whatever the box already holds. */
  onStart(): void;
  /** The session's transcript so far, interim words included. */
  onText(text: string): void;
}

export interface VoiceInput {
  /** The browser has a recogniser and this is a secure context. `false` on the server. */
  supported: boolean;
  /** Recognition will run on this device, so the audio stays on it. */
  onDevice: boolean;
  /** A target is attached, so there is somewhere for a transcript to go. */
  attached: boolean;
  status: VoiceStatus;
  /** Any status but idle: the microphone is, or is about to be, in use. */
  listening: boolean;
  error: VoiceError | null;
  /** Begin a session. A no-op while one runs, or with nothing attached. */
  start(): void;
  /** Finish the session and keep what was heard. */
  stop(): void;
  /** Tear the session down now and deliver nothing more. */
  cancel(): void;
  dismissError(): void;
  /** Register the place transcripts are written; returns the detach. */
  attach(target: VoiceTarget): () => void;
}

const noSubscription = () => () => {};
const getSupported = () =>
  typeof window !== "undefined" &&
  window.isSecureContext !== false &&
  recognitionConstructor() !== null;
const getSupportedOnServer = () => false;

export function useVoiceInput(): VoiceInput {
  // Whether the API exists cannot change while the page is open, so the store never
  // notifies; what it buys is a synchronous answer on the first client render.
  const supported = useSyncExternalStore(noSubscription, getSupported, getSupportedOnServer);
  const [status, setStatus] = useState<VoiceStatus>("idle");
  const [error, setError] = useState<VoiceError | null>(null);
  const [onDevice, setOnDevice] = useState(false);
  const [attached, setAttached] = useState(false);

  const recognitionRef = useRef<Recognition | null>(null);
  const targetRef = useRef<VoiceTarget | null>(null);
  const ceilingRef = useRef<number | null>(null);

  // On-device recognition is asked about once. A browser that has never heard of the
  // question answers "no" by omission, which is the safe reading.
  useEffect(() => {
    const Recognizer = supported ? recognitionConstructor() : null;
    if (!Recognizer?.available) return;
    let live = true;
    Recognizer.available({ langs: [speechLanguage()], processLocally: true })
      .then((availability) => {
        if (live) setOnDevice(availability === "available");
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [supported]);

  const release = useCallback(() => {
    if (ceilingRef.current !== null) {
      window.clearTimeout(ceilingRef.current);
      ceilingRef.current = null;
    }
    recognitionRef.current = null;
  }, []);

  const cancel = useCallback(() => {
    const recognition = recognitionRef.current;
    if (!recognition) return;
    // Detached before aborting, so the abort's own "aborted" error and end event land
    // on nothing, and no word heard after this moment reaches the box.
    recognition.onstart = null;
    recognition.onresult = null;
    recognition.onerror = null;
    recognition.onend = null;
    try {
      recognition.abort();
    } catch {
      // Already over; there is nothing left to stop.
    }
    release();
    setStatus("idle");
  }, [release]);

  const stop = useCallback(() => {
    const recognition = recognitionRef.current;
    if (!recognition) return;
    setStatus("stopping");
    try {
      // Asks for the final result; `onend` follows it and returns the state to idle.
      recognition.stop();
    } catch {
      cancel();
    }
  }, [cancel]);

  const start = useCallback(() => {
    const Recognizer = recognitionConstructor();
    const target = targetRef.current;
    if (!Recognizer || !target || recognitionRef.current) return;

    const lang = speechLanguage();
    const recognition = new Recognizer();
    recognition.lang = lang;
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    if (onDevice) recognition.processLocally = true;

    recognition.onstart = () => {
      setStatus((current) => (current === "starting" ? "listening" : current));
    };
    recognition.onresult = (event) => {
      targetRef.current?.onText(transcriptOf(event.results));
    };
    recognition.onerror = (event) => {
      const described = describeRecognitionError(event.error, lang);
      if (described) setError(described);
    };
    recognition.onend = () => {
      release();
      setStatus("idle");
    };

    recognitionRef.current = recognition;
    setError(null);
    setStatus("starting");
    target.onStart();

    try {
      recognition.start();
    } catch {
      release();
      setStatus("idle");
      setError(describeRecognitionError("failed", lang));
      return;
    }

    ceilingRef.current = window.setTimeout(() => {
      if (recognitionRef.current !== recognition) return;
      setStatus("stopping");
      recognition.stop();
    }, MAX_LISTEN_MS);
  }, [onDevice, release]);

  const dismissError = useCallback(() => setError(null), []);

  const attach = useCallback(
    (target: VoiceTarget) => {
      targetRef.current = target;
      setAttached(true);
      return () => {
        if (targetRef.current !== target) return;
        targetRef.current = null;
        setAttached(false);
        cancel();
      };
    },
    [cancel],
  );

  // A microphone must not outlive the page's attention: a hidden tab or a page being
  // left ends the session. Registered only while one runs.
  useEffect(() => {
    if (status === "idle") return;
    const onVisibility = () => {
      if (document.visibilityState === "hidden") cancel();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", cancel);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", cancel);
    };
  }, [status, cancel]);

  // And not the component either.
  useEffect(() => cancel, [cancel]);

  return useMemo(
    () => ({
      supported,
      onDevice,
      attached,
      status,
      listening: status !== "idle",
      error,
      start,
      stop,
      cancel,
      dismissError,
      attach,
    }),
    [supported, onDevice, attached, status, error, start, stop, cancel, dismissError, attach],
  );
}
