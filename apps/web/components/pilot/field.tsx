import type { InputHTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * A labelled text input.
 *
 * Plain controlled inputs rather than a form library: these forms have a handful of
 * fields and their validation authority is the API, which already returns a typed
 * `validation_failed` envelope. Duplicating those rules client-side means two sources of
 * truth that disagree the moment one changes — and the one the browser holds is the one
 * that cannot be enforced.
 *
 * The label is a real `<label>` with `htmlFor`, the error is wired through
 * `aria-describedby`, and `aria-invalid` marks the control itself. A red border alone
 * conveys nothing to a screen reader, and nothing at all to someone who cannot
 * distinguish the colour.
 */
export function Field({
  id,
  label,
  hint,
  error,
  className,
  ...props
}: {
  id: string;
  label: string;
  hint?: ReactNode;
  error?: string;
} & InputHTMLAttributes<HTMLInputElement>) {
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;

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

      <input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        className={cn(
          "h-11 rounded-xl border bg-surface/40 px-3.5 text-sm text-foreground",
          "placeholder:text-muted-foreground/80",
          "transition-colors duration-200",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
          error ? "border-destructive" : "border-hairline-strong hover:border-hairline",
        )}
        {...props}
      />

      {error ? (
        <p id={errorId} className="text-xs leading-relaxed text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}
