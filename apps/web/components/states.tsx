import { AlertTriangle, Inbox, KeyRound, Lock, type LucideIcon } from "lucide-react";

import { isRetryable, needsSignIn, type Failure } from "@/lib/api-error";
import { cn } from "@/lib/utils";

/**
 * The state taxonomy every data surface needs: loading, empty, error, permission-denied,
 * re-authentication and not-built-yet (§34).
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
 * The one button style these notices use.
 *
 * Not the `Button` primitive: that carries variants this module has no use for, and every
 * notice here wants exactly one affordance rendered exactly one way.
 */
export function NoticeAction({
  onClick,
  children,
}: {
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg border border-hairline-strong px-3.5 py-2 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
    >
      {children}
    </button>
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
        action={onRetry ? <NoticeAction onClick={onRetry}>Try again</NoticeAction> : undefined}
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
 * A classified failure, rendered as whatever that failure actually calls for.
 *
 * This is the point of `classifyApiError`: a surface hands over the error and gets the
 * right screen — a denial reads as a denial rather than as "that did not load", and the
 * retry button appears only where a retry could work. Surfaces that need to place these
 * differently can still reach for the individual components.
 *
 * A 401 is deliberately *not* handled here. It means leave the page, and only the caller
 * knows where to send someone — the shells redirect to sign-in, a widget inside a page
 * may prefer to say so in place. `needsSignIn` is exported so the choice is explicit.
 */
export function FailureState({
  failure,
  onRetry,
  deniedWhat,
}: {
  failure: Failure;
  onRetry?: () => void;
  /** What the caller was trying to do, for the 403 copy. E.g. "reading the audit log". */
  deniedWhat?: string;
}) {
  if (failure.kind === "denied") {
    return <PermissionDenied what={deniedWhat ?? "this"} />;
  }
  if (failure.kind === "auth") {
    return <ReauthRequired />;
  }
  return (
    <ErrorState
      message={failure.message}
      requestId={failure.requestId}
      onRetry={onRetry && isRetryable(failure) ? onRetry : undefined}
    />
  );
}

/**
 * The caller is authenticated and inside the tenant, and may not do this.
 *
 * Says so plainly rather than rendering an empty screen. A 403 dressed as "no results" is
 * the kind of thing that gets diagnosed as a data bug.
 *
 * It used to end "…can change that from Roles & permissions", naming a screen that does
 * not exist — that section is `pending` in `admin-nav.ts`, and `member:assign_role` is
 * declared by no route at all. Directing someone to a door that is not there is worse
 * than not directing them, because they go looking.
 */
export function PermissionDenied({ what }: { what: string }) {
  return (
    <Notice icon={Lock} tone="neutral" title="You do not have access to this">
      <p>
        Your role does not include {what}. An organisation owner or administrator can grant
        it.
      </p>
    </Notice>
  );
}

/**
 * The session is gone and the reader has to prove who they are again.
 *
 * Distinct from `PermissionDenied` on purpose: one is "sign in", the other is "ask
 * someone", and telling a person to get more permissions when their session merely
 * expired sends them to their administrator for nothing.
 *
 * Rendered in place rather than redirecting, for widgets whose surrounding page is still
 * usable. Where the whole surface is dead, the shells redirect instead.
 */
export function ReauthRequired({ onSignIn }: { onSignIn?: () => void } = {}) {
  return (
    <div role="alert">
      <Notice
        icon={KeyRound}
        tone="neutral"
        title="Your session has expired"
        action={onSignIn ? <NoticeAction onClick={onSignIn}>Sign in again</NoticeAction> : undefined}
      >
        <p>
          Sessions end after a period of inactivity, and a signed-out session cannot be
          reused. Signing in again restores exactly the access you had.
        </p>
      </Notice>
    </div>
  );
}

/**
 * The request succeeded and there is genuinely nothing to show.
 *
 * The state most often skipped, and the one whose absence is most expensive: a blank
 * panel is indistinguishable from a failed request, so a reader who sees one goes looking
 * for a bug that is not there. §34 requires it on every surface.
 *
 * `children` rather than a `description` string, because the useful sentence is usually
 * about *why* it is empty — filtered by access, nothing ingested yet, no matches — and
 * that varies per surface in ways one prop cannot carry.
 */
export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <Notice icon={Inbox} tone="neutral" title={title} action={action}>
      {children}
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

export { isRetryable, needsSignIn };
export type { Failure };
