"use client";

import { useId, useState } from "react";
import type { ChangeEvent, KeyboardEvent } from "react";

import { cn } from "@/lib/utils";

/**
 * The six-digit code, shown as six boxes and typed into one field.
 *
 * **One real input behind six painted slots, not six inputs.** The visual requirement
 * and the accessibility requirement pull in opposite directions here, and six separate
 * `<input maxlength="1">` fields lose both halves of the argument: paste of a whole code
 * has to be re-implemented (and usually only works in the first box), `autocomplete`
 * cannot fill six controls, password managers fight it, and a screen reader announces six
 * unlabelled fields where the person was told there is one code. So the control is a
 * single labelled input with `autocomplete="one-time-code"` — which is what makes iOS and
 * Android offer the code from the SMS or mail notification — stretched invisibly across
 * the row, and the boxes underneath are painted from its value and hidden from assistive
 * technology.
 *
 * Everything the mandate asks for then comes from the browser rather than from event
 * handlers that have to be right: paste fills every box because it is one field,
 * backspace walks back because the caret does, auto-advance is the caret moving, and the
 * numeric keyboard comes from `inputMode`. The only behaviour written by hand is
 * discarding non-digits and firing `onComplete` on the last one.
 *
 * The caret is hidden (`caret-transparent`) because a real caret would sit at the row's
 * text origin rather than inside the active box; the active box draws its own.
 */
export function CodeInput({
  id,
  name,
  label,
  hint,
  invalid,
  describedBy,
  length = 6,
  autoFocus,
  disabled,
  onComplete,
  className,
}: {
  id: string;
  name: string;
  label: string;
  hint?: string;
  /** Marks the control rejected. The message itself belongs to the page's one alert. */
  invalid?: boolean;
  /** Id of the element carrying that message, so the field points at it. */
  describedBy?: string;
  length?: number;
  autoFocus?: boolean;
  disabled?: boolean;
  /** Called with the full code the moment the last digit lands. */
  onComplete?: (code: string) => void;
  className?: string;
}) {
  const [value, setValue] = useState("");
  const [focused, setFocused] = useState(false);
  const generated = useId();

  const hintId = hint ? `${id}-hint` : undefined;
  const described = [hintId, describedBy, `${generated}-format`].filter(Boolean).join(" ");

  function onChange(event: ChangeEvent<HTMLInputElement>) {
    // Strip everything that is not a digit *before* it reaches state, so a pasted
    // "123-456" or a code copied with a trailing space still fills the boxes.
    const next = event.target.value.replace(/\D/g, "").slice(0, length);
    setValue(next);
    if (next.length === length) onComplete?.(next);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    // The caret is invisible, so arrow keys would move an insertion point nobody can
    // see and leave the next digit in the middle of the code. Typing is append-only.
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") event.preventDefault();
  }

  const active = Math.min(value.length, length - 1);

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <label htmlFor={id} className="text-sm font-medium text-foreground">
        {label}
      </label>

      {hint ? (
        <p id={hintId} className="text-xs leading-relaxed text-muted-foreground">
          {hint}
        </p>
      ) : null}

      <div className="relative">
        {/* Painted, never read: the input above carries the whole value and the label. */}
        <div aria-hidden="true" className="flex items-center gap-2 sm:gap-3">
          {Array.from({ length }, (_, index) => {
            const filled = index < value.length;
            const isActive = focused && index === active && value.length < length;
            return (
              <div
                key={index}
                data-testid={`code-slot-${index}`}
                data-filled={filled ? "true" : "false"}
                className={cn(
                  "flex h-14 flex-1 items-center justify-center rounded-xl border",
                  "bg-surface/40 font-mono text-xl tabular-nums text-foreground",
                  "transition-colors duration-200",
                  invalid
                    ? "border-destructive"
                    : isActive
                      ? "border-brand ring-2 ring-brand/30"
                      : filled
                        ? "border-hairline"
                        : "border-hairline-strong",
                )}
              >
                {filled ? (
                  value[index]
                ) : isActive ? (
                  <span className="h-6 w-px animate-pulse bg-brand" />
                ) : null}
              </div>
            );
          })}
        </div>

        <input
          id={id}
          name={name}
          value={value}
          onChange={onChange}
          onKeyDown={onKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          type="text"
          inputMode="numeric"
          pattern={`[0-9]{${length}}`}
          autoComplete="one-time-code"
          // Autofocused deliberately: the whole screen exists to receive this code,
          // and landing anywhere else costs every visitor a tab press.
          autoFocus={autoFocus}
          required
          minLength={length}
          // Deliberately no `maxLength`: it truncates the *raw* string the browser
          // receives, so pasting "204-815" would be cut to "204-81" before the handler
          // below ever sees it and the code would arrive as "20481". The handler clamps
          // the digits instead, which is the only length that means anything here.
          disabled={disabled}
          aria-invalid={invalid ? true : undefined}
          aria-describedby={described}
          className={cn(
            "absolute inset-0 h-full w-full rounded-xl bg-transparent text-transparent",
            "caret-transparent outline-none selection:bg-transparent",
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
            disabled ? "cursor-not-allowed" : "cursor-text",
          )}
        />
      </div>

      <p id={`${generated}-format`} className="sr-only">
        {length} digits. You can paste the whole code.
      </p>
    </div>
  );
}
