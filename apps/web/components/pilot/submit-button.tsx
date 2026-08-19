import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * The one submit control for the funnel.
 *
 * `aria-busy` and a live region carry the pending state, not just the spinner: an
 * assistive technology user gets no signal from an icon that starts rotating. The label
 * itself changes too, because `aria-busy` alone is inconsistently announced.
 *
 * Disabled while pending for the obvious reason — these submissions are not idempotent,
 * and a double-click on registration would try to create two organisations.
 */
export function SubmitButton({
  pending,
  pendingLabel,
  children,
  className,
}: {
  pending: boolean;
  pendingLabel: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Button
      type="submit"
      size="lg"
      disabled={pending}
      aria-busy={pending}
      className={cn(
        "h-12 w-full rounded-xl bg-brand px-6 text-[0.9375rem] font-semibold text-brand-foreground",
        "hover:bg-brand/90 focus-visible:ring-brand/40",
        "disabled:opacity-70",
        className,
      )}
    >
      {pending ? (
        <>
          <Loader2 aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" />
          {pendingLabel}
        </>
      ) : (
        children
      )}
    </Button>
  );
}

/**
 * Errors from the API, rendered where a screen reader will hear them.
 *
 * `role="alert"` rather than a plain paragraph: the message appears after a submission
 * the user already committed to, so it has to interrupt. The request id is shown because
 * it is the one thing that makes a support conversation tractable, and it identifies a
 * request rather than a person.
 */
export function FormError({ message, requestId }: { message: string; requestId?: string }) {
  return (
    <div
      role="alert"
      className="rounded-xl border border-destructive/40 bg-destructive/8 p-4 text-sm text-foreground"
    >
      <p className="leading-relaxed">{message}</p>
      {requestId && requestId !== "unknown" ? (
        <p className="mt-2 font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground">
          Reference {requestId}
        </p>
      ) : null}
    </div>
  );
}
