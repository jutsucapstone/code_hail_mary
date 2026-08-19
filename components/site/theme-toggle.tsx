"use client";

import { useSyncExternalStore } from "react";
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
      {OPTIONS.map(({ value, label, Icon }) => {
        const selected = mounted && theme === value;
        return (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={selected}
            aria-label={label}
            title={`${label} theme`}
            // Keep exactly one tab stop for the group, as a radiogroup should.
            tabIndex={selected || (!mounted && value === "system") ? 0 : -1}
            onClick={() => setTheme(value)}
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
