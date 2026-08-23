"use client";

import { useCallback, useSyncExternalStore } from "react";
import { Vibrate, Volume2, VolumeX } from "lucide-react";

import {
  preview,
  readPreference,
  setPreference,
  subscribePreferences,
  supportsHaptics,
  type FeedbackChannel,
} from "@/lib/feedback";
import { cn } from "@/lib/utils";

/**
 * Press-feedback controls: sound, and haptics where the device has them.
 *
 * Two independent toggles rather than one combined switch, because the two channels do
 * not deserve the same default — sound needs asking for, a haptic tick on a phone does
 * not — and a single control cannot express that honestly.
 *
 * Shaped as a sibling of `ThemeToggle` and sharing its container so the chrome reads as
 * one cluster of preferences rather than two competing ones.
 */

/** `localStorage` is a real external store, so it is read as one, not mirrored into state. */
function usePreference(channel: FeedbackChannel): [boolean, (next: boolean) => void] {
  const value = useSyncExternalStore(
    subscribePreferences,
    () => readPreference(channel),
    // The server cannot know a stored preference. Reporting the default keeps the
    // markup deterministic; `useSyncExternalStore` then re-reads on the client.
    () => (channel === "haptics" ? true : false),
  );
  const set = useCallback(
    (next: boolean) => {
      setPreference(channel, next);
      // Answer the question the press just asked. Only on the way on: switching a
      // channel off and then demonstrating it would be absurd.
      if (next) preview(channel);
    },
    [channel],
  );
  return [value, set];
}

/** Never resubscribes — capability is fixed for the life of the document. */
const subscribeNever = () => () => {};

export function FeedbackToggle({ className }: { className?: string }) {
  const [sound, setSound] = usePreference("sound");
  const [haptics, setHaptics] = usePreference("haptics");

  // Feature-detected after hydration. Offering a haptics switch on a laptop — or on an
  // iPhone, where the Vibration API simply does not exist — is offering a control that
  // cannot do anything, which is worse than not offering one.
  const canVibrate = useSyncExternalStore(subscribeNever, supportsHaptics, () => false);

  return (
    <div
      // Not a radiogroup: these are two independent switches, not one choice among
      // several, so each carries its own pressed state.
      role="group"
      aria-label="Press feedback"
      className={cn(
        "inline-flex items-center gap-0.5 rounded-lg border border-hairline bg-surface/60 p-0.5",
        className,
      )}
    >
      <button
        type="button"
        aria-pressed={sound}
        // The label states what pressing it will do, not what the icon depicts — the
        // icon already carries the current state visually.
        aria-label={sound ? "Turn press sound off" : "Turn press sound on"}
        title={sound ? "Press sound on" : "Press sound off"}
        onClick={() => setSound(!sound)}
        className={cn(
          "inline-flex size-7 items-center justify-center rounded-md transition-colors duration-200",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
          sound
            ? "bg-brand/12 text-brand"
            : "text-muted-foreground hover:bg-accent hover:text-foreground",
        )}
      >
        {sound ? (
          <Volume2 aria-hidden="true" className="size-3.5" />
        ) : (
          <VolumeX aria-hidden="true" className="size-3.5" />
        )}
      </button>

      {canVibrate ? (
        <button
          type="button"
          aria-pressed={haptics}
          aria-label={haptics ? "Turn haptics off" : "Turn haptics on"}
          title={haptics ? "Haptics on" : "Haptics off"}
          onClick={() => setHaptics(!haptics)}
          className={cn(
            "inline-flex size-7 items-center justify-center rounded-md transition-colors duration-200",
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
            haptics
              ? "bg-brand/12 text-brand"
              : "text-muted-foreground hover:bg-accent hover:text-foreground",
          )}
        >
          <Vibrate aria-hidden="true" className="size-3.5" />
        </button>
      ) : null}
    </div>
  );
}
