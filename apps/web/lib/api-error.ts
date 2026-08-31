import { ApiError } from "@/lib/api";

/**
 * One reading of a failed request, shared by every surface.
 *
 * This was private to `components/product/evidence-search.tsx`. The moment a second
 * surface needed to tell "your session expired" apart from "you may not do that", the
 * choice was one vocabulary or two — and two is how a 429 grows a "Try again" button on
 * one screen and not on another, for no reason a reader could infer.
 *
 * **Keyed on status, never on `code`.** The status is the contract the API commits to in
 * §15; `code` is free to gain new values inside an existing status, and a lookup on it
 * would drop those into "unexpected" while the status already said exactly what to do.
 */
export type FailureKind =
  /** 401 — the session is gone. Sign in again; retrying cannot help. */
  | "auth"
  /** 403 — authenticated, inside the tenant, and not permitted. A decision, not weather. */
  | "denied"
  /** 404 — no such thing, or nothing the caller may see. Often a normal empty state. */
  | "missing"
  /** 422 — the input was refused. The reader must change it. */
  | "invalid"
  /** 429 — a budget is spent. Retrying sooner is how a rate limit becomes an outage. */
  | "throttled"
  /** 503 — a dependency is down. Worth retrying. */
  | "unavailable"
  /** Anything else, including a transport fault with no envelope at all. */
  | "other";

export interface Failure {
  kind: FailureKind;
  message: string;
  /** Present whenever the API answered. Identifies a request, never a person (§4.9). */
  requestId?: string;
  /** HTTP status, or `undefined` when the request never reached the API. */
  status?: number;
}

/**
 * Whether offering "Try again" is honest.
 *
 * A retry button that cannot work is worse than no button: it teaches people that the
 * control does nothing, and they stop believing the one that would have helped. Only
 * transport faults, server errors and spent budgets can change on a second attempt —
 * and a 401, 403 or 422 will answer identically for ever.
 */
export function isRetryable(failure: Failure): boolean {
  return (
    failure.kind === "throttled" ||
    failure.kind === "unavailable" ||
    failure.kind === "other"
  );
}

/** A 401 is the one failure that means "leave this page", not "show a message". */
export function needsSignIn(failure: Failure): boolean {
  return failure.kind === "auth";
}

export function classifyApiError(error: unknown): Failure {
  if (!(error instanceof ApiError)) {
    // No envelope means the request never reached the API — offline, DNS, a proxy fault.
    // The reader gets something renderable rather than an undefined property access.
    return { kind: "other", message: "Something went wrong. Please try again." };
  }

  const requestId = error.requestId;
  const status = error.status;

  switch (status) {
    case 401:
      // The API's own message here is deliberately uninformative, so this one sentence is
      // written rather than forwarded.
      return {
        kind: "auth",
        message: "Your session has expired. Sign in again to continue.",
        requestId,
        status,
      };
    case 403:
      return { kind: "denied", message: error.message, requestId, status };
    case 404:
      return { kind: "missing", message: error.message, requestId, status };
    case 422:
      return { kind: "invalid", message: error.message, requestId, status };
    case 429:
      return { kind: "throttled", message: error.message, requestId, status };
    case 503:
      return { kind: "unavailable", message: error.message, requestId, status };
    default:
      return { kind: "other", message: error.message, requestId, status };
  }
}
