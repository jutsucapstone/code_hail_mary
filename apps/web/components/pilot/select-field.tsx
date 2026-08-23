import type { ReactNode, SelectHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

/**
 * A labelled `<select>`, matching `Field` exactly.
 *
 * The organisation form had one hand-rolled select and now needs three. Three copies of
 * the same eleven Tailwind classes is how two of them end up a pixel different and
 * neither gets the focus ring — so the pattern moves here, beside the input it has to
 * line up with.
 *
 * A native select on purpose. It is keyboard accessible, works with screen readers, and
 * on a phone opens the platform picker — all things a styled listbox has to reimplement
 * and usually reimplements incompletely.
 */
export function SelectField({
  id,
  label,
  hint,
  error,
  className,
  placeholder,
  children,
  ...props
}: {
  id: string;
  label: string;
  hint?: ReactNode;
  error?: string;
  /** Rendered as a disabled, selected-by-default first option. */
  placeholder: string;
  children: ReactNode;
} & SelectHTMLAttributes<HTMLSelectElement>) {
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

      <select
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        defaultValue=""
        className={cn(
          "h-11 rounded-xl border bg-surface/40 px-3.5 text-sm text-foreground",
          "transition-colors duration-200",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
          error ? "border-destructive" : "border-hairline-strong hover:border-hairline",
        )}
        {...props}
      >
        {/* Disabled rather than merely empty, so the placeholder cannot be submitted as
            a value — and `required` then does the right thing for the fields that need
            an answer. */}
        <option value="" disabled>
          {placeholder}
        </option>
        {children}
      </select>

      {error ? (
        <p id={errorId} className="text-xs leading-relaxed text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}
