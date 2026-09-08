"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy, X } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Copy a short value — a JUTSU ID, a KT code — and say honestly whether it worked.
 *
 * **The confirmation waits for the clipboard.** `navigator.clipboard.writeText` returns a
 * promise that rejects on a page the browser does not consider secure, in a cross-origin
 * frame, when the document is not focused, and whenever the user has denied clipboard
 * access. Firing it with `void` and immediately reporting success — which is what every
 * call site here used to do — tells somebody their JUTSU ID is on the clipboard when it
 * is not, and they discover the lie by pasting nothing into a sign-in field. So the
 * promise is awaited, and a rejection says so.
 *
 * **A failure is recoverable, not a dead end.** The value is selected in place when the
 * copy fails, so the reader can press the shortcut themselves rather than transcribing
 * eight characters by eye.
 *
 * **The accessible name never changes.** The visible label swaps to "Copied" for two
 * seconds, but a button whose accessible name changes under focus is re-announced as a
 * different control; the result is announced once, by the live region, which is what a
 * screen-reader user actually needs to hear.
 */
export function CopyButton({
  value,
  label,
  children,
  className,
  onCopied,
}: {
  /** The exact text to place on the clipboard. */
  value: string;
  /** The stable accessible name, e.g. `Copy KT ID JUTSU-KT-4RCXQ2`. */
  label: string;
  /** The visible resting label. Defaults to "Copy". */
  children?: React.ReactNode;
  className?: string;
  /** For a caller that wants to react — a toast, a focus move. Never used for feedback. */
  onCopied?: () => void;
}) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const holder = useRef<HTMLElement>(null);

  // Cleared on unmount, or a state update lands on a component that is gone — React
  // warns, and in a table whose rows come and go it warns constantly.
  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  // **After the render that creates it, not inside the click.** The fallback only exists
  // in the failed state, so selecting it from the handler would run against a node React
  // has not mounted yet and silently do nothing.
  useEffect(() => {
    if (state !== "failed" || !holder.current) return;
    const range = document.createRange();
    range.selectNodeContents(holder.current);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  }, [state]);

  function settle(next: "copied" | "failed") {
    setState(next);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 2000);
  }

  async function copy() {
    try {
      // **Not `navigator.clipboard?.writeText(value)`.** On an insecure origin the whole
      // `clipboard` object is undefined, and optional chaining makes that expression
      // evaluate to `undefined` — which `await` resolves happily, so the button would
      // report "Copied" on precisely the browsers that copied nothing. The absence has
      // to become a failure explicitly.
      const clipboard = navigator.clipboard;
      if (!clipboard?.writeText) throw new Error("this browser exposes no clipboard");
      await clipboard.writeText(value);
      settle("copied");
      onCopied?.();
    } catch {
      // The effect above selects the fallback once it exists.
      setState("failed");
      if (timer.current) clearTimeout(timer.current);
    }
  }

  const Icon = state === "copied" ? Check : state === "failed" ? X : Copy;

  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={copy}
        aria-label={label}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-lg border border-hairline-strong px-3 py-1.5 text-xs font-medium transition-colors",
          "hover:border-brand/40 hover:bg-brand/5",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
          state === "copied" ? "border-brand/50 text-brand" : "",
          state === "failed" ? "border-destructive/50 text-destructive" : "",
          className,
        )}
      >
        <Icon aria-hidden="true" className="size-3.5" />
        {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : (children ?? "Copy")}
      </button>
      {/* The recovery path, and only when it is needed.
          Rendering the value at rest — even visually hidden — would put it in the
          accessibility tree twice everywhere this sits beside the value it copies, so a
          screen reader would read somebody's JUTSU ID out and then read it again. */}
      {state === "failed" ? (
        <code
          ref={holder}
          className="rounded border border-destructive/40 bg-destructive/8 px-1.5 py-0.5 font-mono text-xs text-foreground select-all"
        >
          {value}
        </code>
      ) : null}
      {/* Announced once, and only on a real outcome. The empty resting value is what
          stops a screen reader repeating the last result on every unrelated re-render. */}
      <span role="status" aria-live="polite" className="sr-only">
        {state === "copied"
          ? `${label}: copied.`
          : state === "failed"
            ? `${label}: could not copy. The value is selected — use your copy shortcut.`
            : ""}
      </span>
    </span>
  );
}
