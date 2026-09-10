"use client";

import { useState, type ReactNode } from "react";

import { AskExperience } from "@/components/product/ask-experience";
import { VoicePoweredOrb } from "@/components/ui/voice-powered-orb";
import { useMediaQuery, WIDE_QUERY } from "@/lib/use-media-query";
import { useVoiceInput, type VoiceInput } from "@/lib/voice-input";

/**
 * Cited Q&A's layout: the page heading and the question box on the left, and on a wide
 * screen the voice orb beside them.
 *
 * It exists to own voice input, which both halves read: the box writes the transcript
 * and the orb shows the listening. One recogniser, one owner, passed down — so the
 * orb and the microphone button can never disagree about whether the microphone is
 * open.
 *
 * The orb appears only when speaking can do something: the browser has a recogniser
 * and the question box has attached itself as the place a transcript goes. On a
 * deployment with no answer provider the box turns into evidence search and detaches,
 * and the orb goes with it. Below `lg` there is no second column; a small orb shows in
 * the box's status line while listening instead — never both, so there is only ever
 * one WebGL context and one extra microphone reader.
 */
export function AskWorkspace({ children }: { children: ReactNode }) {
  const voice = useVoiceInput();
  const wide = useMediaQuery(WIDE_QUERY);

  return (
    <div className="grid gap-12 lg:grid-cols-[minmax(0,48rem)_minmax(0,1fr)] lg:items-start">
      <div className="min-w-0 max-w-3xl">
        {children}
        <AskExperience voice={voice} inlineOrb={!wide} />
      </div>
      {wide && voice.supported && voice.attached ? <VoiceOrbPanel voice={voice} /> : null}
    </div>
  );
}

/**
 * The big orb, which is also a microphone button.
 *
 * Its caption is hidden from assistive technology: the status line under the question
 * box announces the same state, and the button's own label names the action.
 */
function VoiceOrbPanel({ voice }: { voice: VoiceInput }) {
  const [hearing, setHearing] = useState(false);
  const active = voice.listening;

  return (
    <div className="flex flex-col items-center gap-5 pt-4">
      <button
        type="button"
        onClick={() => (active ? voice.stop() : voice.start())}
        disabled={voice.status === "stopping"}
        aria-label={active ? "Stop voice input" : "Ask by voice"}
        className="relative size-72 rounded-full transition-transform focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand disabled:cursor-wait motion-safe:hover:scale-[1.02] xl:size-80"
      >
        <VoicePoweredOrb
          enableVoiceControl={active}
          onVoiceDetected={setHearing}
          className="size-full"
        />
      </button>
      <p
        aria-hidden="true"
        className="max-w-60 text-center font-mono text-[0.625rem] uppercase tracking-[0.16em] text-muted-foreground"
      >
        {active ? (hearing ? "Hearing you" : "Listening") : "Press the orb to ask by voice"}
      </p>
    </div>
  );
}
