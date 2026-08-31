"use client";

import { useCallback, useEffect, useState } from "react";
import { KeyRound, Loader2 } from "lucide-react";

import { ErrorState, LoadingRegion, PermissionDenied, Skeleton } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError, api, type SourceIdentity } from "@/lib/api";
import type { components } from "@/lib/api-schema";
import { can, type Capabilities } from "@/lib/permissions";

type SourceSystem = components["schemas"]["SourceSystem"];
type Employee = components["schemas"]["Employee"];

/**
 * Source identities — **not** integrations, and the difference is the whole point.
 *
 * A source identity is the namespaced provider subject, `{source_system}:{subject}`,
 * that `document_acl` grants are written against. Linking one is therefore **the act of
 * granting document access**; revoking one takes that access away on the very next
 * query, with no cache to flush. There is no OAuth here, no token, and no content fetch —
 * that is connector management, which does not exist in this repository at all.
 *
 * Calling this screen "Integrations" would invite somebody to wire a "Disconnect" button
 * to `DELETE /v1/employees/{id}/identities/{sid}` believing they were unplugging a data
 * feed, when what they actually did was remove a colleague's access to documents. The
 * copy on this page exists to make that impossible to misread.
 *
 * **Rendering is gated on permissions; nothing here is the enforcement.** Every endpoint
 * re-checks server-side, and the API additionally refuses to let an administrator link a
 * subject to their own account — a refusal that is deliberately *not* a permission check,
 * because an Owner holds every permission and gating it on one would make it no refusal
 * at all.
 */

/** Every system the API's `SourceSystem` enum accepts, in the order it declares them. */
const SOURCE_SYSTEMS: readonly SourceSystem[] = [
  "local",
  "gmail",
  "m365",
  "slack",
  "jira",
  "confluence",
  "github",
] as const;

function principal(identity: SourceIdentity): string {
  // Exactly the string `document_acl.principal_id` holds. Showing the assembled form
  // rather than the two parts is the point: this is what a grant is matched against.
  return `${identity.source_system}:${identity.subject}`;
}

function LinkedBy({ value }: { value: string }) {
  // `verified_email` means registration or invitation acceptance created it from an
  // address somebody actually proved they controlled. `admin` means a human decided.
  // Those carry different weight in an audit conversation, so they read differently.
  const label = value === "verified_email" ? "Verified email" : value === "admin" ? "Administrator" : value;
  return (
    <span className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted-foreground">
      {label}
    </span>
  );
}

export function SourceIdentities({
  employee,
  capabilities,
}: {
  employee: Employee;
  capabilities: Capabilities;
}) {
  const [identities, setIdentities] = useState<SourceIdentity[] | null>(null);
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(null);

  const [system, setSystem] = useState<SourceSystem>("local");
  const [subject, setSubject] = useState("");
  const [linking, setLinking] = useState(false);
  const [linkError, setLinkError] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);

  const mayRead = can(capabilities, "integration:read");
  const mayLink = can(capabilities, "integration:connect");
  const mayRevoke = can(capabilities, "integration:revoke");
  const isSelf = capabilities.user_id === employee.id;

  // Nothing set synchronously — see the note in `app/admin/identities/page.tsx`.
  const load = useCallback(() => {
    api
      .employeeIdentities(employee.id)
      .then((page) => {
        setIdentities(page.items);
        setFailure(null);
      })
      .catch((error: unknown) => {
        setIdentities([]);
        setFailure({
          message: error instanceof ApiError ? error.message : "That did not load.",
          requestId: error instanceof ApiError ? error.requestId : undefined,
        });
      });
  }, [employee.id]);

  useEffect(() => {
    if (mayRead) load();
  }, [load, mayRead]);

  const link = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      const value = subject.trim();
      if (!value || linking) return;
      setLinking(true);
      setLinkError(null);
      try {
        await api.linkIdentity(employee.id, { source_system: system, subject: value });
        setSubject("");
        load();
      } catch (error) {
        setLinkError(error instanceof ApiError ? error.message : "That did not work.");
      } finally {
        setLinking(false);
      }
    },
    [employee.id, linking, load, subject, system],
  );

  const revoke = useCallback(
    async (identity: SourceIdentity) => {
      setRevoking(identity.id);
      setLinkError(null);
      try {
        await api.revokeIdentity(employee.id, identity.id);
        load();
      } catch (error) {
        setLinkError(error instanceof ApiError ? error.message : "That did not work.");
      } finally {
        setRevoking(null);
      }
    },
    [employee.id, load],
  );

  if (!mayRead) return <PermissionDenied what="source identities" />;

  return (
    <section className="mt-10">
      <h2 className="display text-lg font-semibold sm:text-xl">Source identities</h2>
      <p className="mt-2 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
        The provider accounts this person is known by. Document permissions are granted to
        these identities, so linking one <strong className="font-medium text-foreground">grants
        access</strong> to every document already shared with that account, and revoking one
        removes it on the next search. This is not an application connection — no content is
        fetched here.
      </p>

      {identities === null ? (
        <LoadingRegion label="Loading source identities">
          <div className="mt-6 space-y-2">
            {[0, 1].map((n) => (
              <Skeleton key={n} className="h-12 w-full" />
            ))}
          </div>
        </LoadingRegion>
      ) : failure ? (
        <div className="mt-6">
          <ErrorState message={failure.message} requestId={failure.requestId} onRetry={load} />
        </div>
      ) : identities.length === 0 ? (
        <div className="mt-6 rounded-2xl border border-hairline bg-surface/40 p-6">
          <p className="eyebrow text-muted-foreground/80">No linked identities</p>
          <p className="mt-3 text-pretty text-sm leading-relaxed text-muted-foreground">
            This person is not known by any provider account yet, so no document grants
            match them. Searches will correctly return nothing.
          </p>
        </div>
      ) : (
        <div className="mt-6 overflow-x-auto rounded-2xl border border-hairline">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Principal</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Linked by</TableHead>
                {mayRevoke ? <TableHead className="text-right">Action</TableHead> : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {identities.map((identity) => (
                <TableRow key={identity.id}>
                  <TableCell className="font-mono text-xs">{principal(identity)}</TableCell>
                  <TableCell>
                    {/* The word is the signal; the tint only reinforces it. A
                        colour-only status is unreadable to a screen reader and to
                        anyone who cannot distinguish the hue. */}
                    <Badge variant={identity.is_active ? "default" : "secondary"}>
                      {identity.is_active ? "Active" : "Revoked"}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <LinkedBy value={identity.linked_by} />
                  </TableCell>
                  {mayRevoke ? (
                    <TableCell className="text-right">
                      {identity.is_active ? (
                        <button
                          type="button"
                          onClick={() => void revoke(identity)}
                          disabled={revoking === identity.id}
                          className="inline-flex items-center gap-1.5 rounded-lg border border-hairline-strong px-3 py-1.5 text-xs transition-colors hover:border-destructive/40 hover:bg-destructive/5 disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                        >
                          {revoking === identity.id ? (
                            <Loader2
                              aria-hidden="true"
                              className="size-3 animate-spin motion-reduce:animate-none"
                            />
                          ) : null}
                          Revoke access
                        </button>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </TableCell>
                  ) : null}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {mayLink ? (
        <form onSubmit={link} className="mt-8 rounded-2xl border border-hairline bg-surface/40 p-6">
          <p className="eyebrow flex items-center gap-2 text-muted-foreground/80">
            <KeyRound aria-hidden="true" className="size-3.5" />
            Link an identity
          </p>

          {isSelf ? (
            // Rendered rather than hidden: the reader should learn the rule, not
            // discover a missing form. The API refuses this regardless of what the
            // browser does — and refuses it for an Owner too, which is the point.
            <p className="mt-3 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
              You cannot link an identity to your own account. Another administrator must
              do it. This is not a permission you can be granted — an administrator who
              could grant themselves document access would be no restriction at all.
            </p>
          ) : (
            <>
              <p className="mt-3 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
                Linking grants this person access to every document already shared with
                that account. Use the subject the source system issues — for a local
                corpus that is the address on the message.
              </p>

              <div className="mt-5 flex flex-col gap-3 sm:flex-row">
                <label className="sr-only" htmlFor="identity-system">
                  Source system
                </label>
                <select
                  id="identity-system"
                  value={system}
                  onChange={(event) => setSystem(event.target.value as SourceSystem)}
                  className="rounded-xl border border-hairline bg-background px-3 py-2.5 text-sm focus-visible:border-brand/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                >
                  {SOURCE_SYSTEMS.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>

                <label className="sr-only" htmlFor="identity-subject">
                  Subject
                </label>
                <input
                  id="identity-subject"
                  value={subject}
                  onChange={(event) => setSubject(event.target.value)}
                  maxLength={255}
                  placeholder="the subject this system issues"
                  className="flex-1 rounded-xl border border-hairline bg-background px-3 py-2.5 text-sm placeholder:text-muted-foreground/70 focus-visible:border-brand/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                />

                <button
                  type="submit"
                  disabled={linking || subject.trim().length === 0}
                  className="inline-flex shrink-0 items-center justify-center gap-2 rounded-xl border border-hairline-strong px-5 py-2.5 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                >
                  {linking ? (
                    <Loader2
                      aria-hidden="true"
                      className="size-4 animate-spin motion-reduce:animate-none"
                    />
                  ) : null}
                  Link identity
                </button>
              </div>
            </>
          )}

          {linkError ? (
            <p role="alert" className="mt-4 text-sm text-destructive">
              {linkError}
            </p>
          ) : null}
        </form>
      ) : null}
    </section>
  );
}
