"use client";

import { Mic, Square } from "lucide-react";

import { VoicePoweredOrb } from "@/components/ui/voice-powered-orb";
import { cn } from "@/lib/utils";
import type { VoiceInput } from "@/lib/voice-input";

/**
 * Voice input's two pieces inside the question box: the microphone control, and the
 * line that says what it is doing.
 *
 * The line is the only place voice state is announced. The orb beside the box on a
 * wide screen shows the same thing, and says nothing to a screen reader, so nobody
 * hears "Listening" twice.
 */

export function VoiceButton({
  voice,
  disabled,
  className,
}: {
  voice: VoiceInput;
  disabled?: boolean;
  className?: string;
}) {
  const active = voice.listening;
  const label = active ? "Stop voice input" : "Ask by voice";

  return (
    <button
      type="button"
      onClick={() => (active ? voice.stop() : voice.start())}
      disabled={disabled || voice.status === "stopping"}
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex size-9 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-brand/10 hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50",
        active && "bg-brand/15 text-brand hover:bg-brand/20 hover:text-brand",
        className,
      )}
    >
      {active ? (
        <Square aria-hidden="true" className="size-3.5 fill-current" />
      ) : (
        <Mic aria-hidden="true" className="size-4" />
      )}
    </button>
  );
}

/** The sentence for the status line. Empty when there is nothing to say. */
export function voiceStatusMessage(voice: VoiceInput): string {
  if (voice.error) return voice.error.message;
  switch (voice.status) {
    case "starting":
      return "Starting the microphone…";
    case "listening":
      return `Listening — ask your question, then pause. ${
        voice.onDevice
          ? "Transcribed on this device."
          : "Your browser's speech service turns it into text."
      } Press Escape to cancel.`;
    case "stopping":
      return "Finishing what you said…";
    default:
      return "";
  }
}

export function VoiceStatus({
  id,
  voice,
  showOrb,
}: {
  id: string;
  voice: VoiceInput;
  /** Draw a small orb beside the words, for the screens with no room for the big one. */
  showOrb: boolean;
}) {
  const message = voiceStatusMessage(voice);

  return (
    <div className={cn("flex items-center gap-3", !message && "hidden")}>
      {showOrb && voice.listening ? (
        <VoicePoweredOrb enableVoiceControl className="size-12 shrink-0" />
      ) : null}
      {/* Mounted from the first render, like the answer announcer below it: a live
          region added at the moment its text appears is often not read at all. */}
      <p
        id={id}
        role="status"
        aria-live="polite"
        className={cn(
          "text-pretty text-xs leading-relaxed",
          voice.error ? "text-foreground" : "text-muted-foreground",
        )}
      >
        {message}
      </p>
    </div>
  );
}
