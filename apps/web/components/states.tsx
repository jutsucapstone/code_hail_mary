import { AlertTriangle, Lock, type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * The state taxonomy every data surface needs: loading, empty, error, permission-denied
 * and not-built-yet (§34).
 *
 * Lives here rather than under `components/admin/` because it never was admin-specific —
 * `components/product/evidence-search.tsx` imported it across that boundary from the day
 * it was written. A shared vocabulary of states filed under one consumer is a shared
 * vocabulary nobody else finds.
 *
 * `Skeleton` is re-exported from `components/ui/skeleton.tsx` rather than defined twice.
 * The shadcn primitive there carries the `aria-hidden` and `motion-reduce` handling this
 * module used to own, so there is one placeholder box in the app and not two that drift.
 */

export { Skeleton } from "@/components/ui/skeleton";

/**
 * A loading region that announces itself.
 *
 * The skeleton is decorative, so it is `aria-hidden`; the status text is what a screen
 * reader hears. Without it the page is simply silent while it loads, which is
 * indistinguishable from nothing happening.
 */
export function LoadingRegion({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div role="status" aria-live="polite">
      <span className="sr-only">{label}</span>
      {children}
    </div>
  );
}

function Notice({
  icon: Icon,
  tone,
  title,
  children,
  action,
}: {
  icon: LucideIcon;
  tone: "neutral" | "warning";
  title: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-start gap-4 rounded-2xl border p-6 sm:p-8",
        tone === "warning"
          ? "border-destructive/40 bg-destructive/8"
          : "border-hairline bg-surface/40",
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "flex size-10 items-center justify-center rounded-xl border",
          tone === "warning"
            ? "border-destructive/40 text-destructive"
            : "border-hairline-strong bg-surface text-brand",
        )}
      >
        <Icon className="size-5" />
      </span>

      <div>
        <h2 className="display text-lg font-semibold sm:text-xl">{title}</h2>
        <div className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          {children}
        </div>
      </div>

      {action}
    </div>
  );
}

/**
 * Something failed. `role="alert"` because it replaces content the reader asked for.
 *
 * The request id is shown deliberately: it is what makes a support conversation
 * tractable, and it identifies a request rather than a person.
 */
export function ErrorState({
  message,
  requestId,
  onRetry,
}: {
  message: string;
  requestId?: string;
  onRetry?: () => void;
}) {
  return (
    <div role="alert">
      <Notice
        icon={AlertTriangle}
        tone="warning"
        title="That did not load"
        action={
          onRetry ? (
            <button
              type="button"
              onClick={onRetry}
              className="rounded-lg border border-hairline-strong px-3.5 py-2 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
            >
              Try again
            </button>
          ) : undefined
        }
      >
        <p>{message}</p>
        {requestId && requestId !== "unknown" ? (
          <p className="mt-2 font-mono text-[0.6875rem] uppercase tracking-[0.14em]">
            Reference {requestId}
          </p>
        ) : null}
      </Notice>
    </div>
  );
}

/**
 * The caller is authenticated and inside the tenant, and may not do this.
 *
 * Says so plainly rather than rendering an empty screen. A 403 dressed as "no results" is
 * the kind of thing that gets diagnosed as a data bug.
 */
export function PermissionDenied({ what }: { what: string }) {
  return (
    <Notice icon={Lock} tone="neutral" title="You do not have access to this">
      <p>
        Your role does not include {what}. An organisation owner or administrator can
        change that from Roles &amp; permissions.
      </p>
    </Notice>
  );
}

/**
 * A section that is routed but has no backend yet.
 *
 * §4.11 forbids mock data behind a UI surface, so this names what is missing and which
 * slice supplies it, rather than rendering plausible figures that would be
 * indistinguishable from real ones in a screenshot.
 */
export function NotBuiltYet({ name, slice }: { name: string; slice: string }) {
  return (
    <Notice icon={AlertTriangle} tone="neutral" title={`${name} is not built yet`}>
      <p>
        This section is routed and reachable, but nothing is behind it. It lands in slice{" "}
        <span className="font-mono text-foreground">{slice}</span>. Nothing here is mocked —
        when it shows numbers, those numbers will be real.
      </p>
    </Notice>
  );
}
