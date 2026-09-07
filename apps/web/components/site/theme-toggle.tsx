"use client";

import { useRef, useSyncExternalStore } from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";

import { cn } from "@/lib/utils";

const OPTIONS = [
  { value: "light", label: "Light", Icon: Sun },
  { value: "dark", label: "Dark", Icon: Moon },
  { value: "system", label: "System", Icon: Monitor },
] as const;

/**
 * Three-state theme control as a radiogroup: Light / Dark / System.
 *
 * "System" is a real, selectable state rather than just the initial default —
 * a two-way toggle silently strips a visitor's ability to hand control back to
 * their OS once they've touched it.
 *
 * Until mounted, `theme` is unknown on the client (the server cannot know it),
 * so the control renders in a neutral, non-committal state and is marked busy.
 * Rendering a guessed selection would show the wrong pip on first paint.
 */
/** Never resubscribes — the value flips once, at hydration, and stays. */
const subscribeNever = () => () => {};

export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  // `false` on the server, `true` after hydration, with no setState-in-effect.
  const mounted = useSyncExternalStore(
    subscribeNever,
    () => true,
    () => false,
  );

  return (
    <div
      role="radiogroup"
      aria-label="Colour theme"
      aria-busy={!mounted}
      className={cn(
        "inline-flex items-center gap-0.5 rounded-lg border border-hairline bg-surface/60 p-0.5",
        className,
      )}
    >
      {OPTIONS.map(({ value, label, Icon }, index) => {
        const selected = mounted && theme === value;
        return (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={selected}
            aria-label={label}
            title={`${label} theme`}
            ref={(node) => {
              buttons.current[index] = node;
            }}
            // Keep exactly one tab stop for the group, as a radiogroup should.
            tabIndex={selected || (!mounted && value === "system") ? 0 : -1}
            onClick={() => setTheme(value)}
            // **The arrow keys are not a nicety here, they are the only way in.**
            // A roving tabindex gives the group one tab stop, so Tab reaches the
            // selected option and nothing else. Without a key handler the other two
            // options were unreachable by keyboard entirely — WCAG 2.1.1, on the
            // control that decides whether the whole product is legible to someone who
            // needs a dark or a light ground.
            onKeyDown={(event) => {
              const forward = event.key === "ArrowRight" || event.key === "ArrowDown";
              const back = event.key === "ArrowLeft" || event.key === "ArrowUp";
              if (!forward && !back) return;
              event.preventDefault();
              const step = forward ? 1 : -1;
              const next = (index + step + OPTIONS.length) % OPTIONS.length;
              // Selection follows focus, which is the radiogroup pattern: moving
              // through the options previews each theme as it is reached.
              setTheme(OPTIONS[next].value);
              buttons.current[next]?.focus();
            }}
            className={cn(
              "inline-flex size-7 items-center justify-center rounded-md transition-colors duration-200",
              "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
              selected
                ? "bg-brand/12 text-brand"
                : "text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            <Icon aria-hidden="true" className="size-3.5" />
          </button>
        );
      })}
    </div>
  );
}
