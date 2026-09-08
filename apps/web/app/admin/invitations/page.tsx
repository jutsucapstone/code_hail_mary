"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";

import { useCapabilities } from "@/components/admin/admin-shell";
import {
  LoadMore,
  PageHeader,
  Pill,
  TableShell,
  When,
} from "@/components/admin/page-scaffold";
import {
  EmptyState,
  FailureState,
  LoadingRegion,
  PermissionDenied,
  Skeleton,
} from "@/components/states";
import { api, type InvitationPage } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import { ROLE_LABELS, can } from "@/lib/permissions";

/**
 * Invitations — what happened to every one this organisation sent, and the two things
 * an administrator can do about it.
 *
 * The addressee's email is shown deliberately: the caller holds `member:invite`, and an
 * invitation *is* an email address. Status is derived server-side against the database
 * clock, so "expired" here and "expired" at acceptance time cannot disagree.
 *
 * **Cancel and Resend are offered on the rows the server would accept them for**, which
 * is a courtesy and not the enforcement — both routes take `member:invite` and refuse an
 * invitation that is no longer waiting. Resend re-checks the rank ceiling against the
 * person pressing it, so an HR Admin cannot resend a Super Admin's invitation at Super
 * Admin level even by crafting the request.
 *
 * Both refetch rather than patching the row in place. The status shown is derived from
 * the database clock, and a locally-edited copy would be the one thing on this page that
 * was not.
 */

function InvitationStatus({ status }: { status: string }) {
  const tone =
    status === "accepted"
      ? "good"
      : status === "pending"
        ? "attention"
        : status === "revoked"
          ? "bad"
          : "neutral";
  return <Pill tone={tone}>{status}</Pill>;
}

export default function InvitationsPage() {
  const capabilities = useCapabilities();
  const [older, setOlder] = useState<InvitationPage["items"]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  // Distinct from `cursor === null`, which is also the state before any walk: without
  // it the null cursor falls back to the head page's cursor and the walk restarts.
  const [exhausted, setExhausted] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const mayRead = can(capabilities, "member:invite");
  // Which row is mid-request, so its own buttons disable rather than the whole table
  // freezing. `null` while nothing is in flight.
  const [acting, setActing] = useState<string | null>(null);

  const head = useQuery({
    queryKey: ["invitations"],
    queryFn: () => api.invitations(),
    enabled: mayRead,
  });

  if (!mayRead) {
    return <PermissionDenied what="permission to see invitations" />;
  }

  async function loadOlder() {
    const next = cursor ?? head.data?.next_cursor;
    if (!next) return;
    setLoadingMore(true);
    try {
      const page = await api.invitations({ cursor: next });
      setOlder((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
      if (page.next_cursor === null) setExhausted(true);
    } catch (error) {
      toast.error(classifyApiError(error).message);
    } finally {
      setLoadingMore(false);
    }
  }

  async function act(
    invitation: InvitationPage["items"][number],
    what: "revoke" | "resend",
  ) {
    setActing(invitation.id);
    try {
      if (what === "revoke") {
        await api.revokeInvitation(invitation.id);
        toast.success(`Invitation to ${invitation.email} cancelled.`);
      } else {
        await api.resendInvitation(invitation.id);
        toast.success(`A new invitation is on its way to ${invitation.email}.`);
      }
      // The walked-back pages are dropped rather than merged: a resend creates a row and
      // cancels another, so the older pages the reader had accumulated no longer line up
      // with the cursor they came from.
      setOlder([]);
      setCursor(null);
      setExhausted(false);
      await head.refetch();
    } catch (error) {
      toast.error(classifyApiError(error).message);
    } finally {
      setActing(null);
    }
  }

  const rows = [...(head.data?.items ?? []), ...older];
  const more = !exhausted && (cursor ?? head.data?.next_cursor);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="People" title="Invitations">
        Every invitation this organisation has sent, newest first. Sending one is done
        from Employees; this is where you see whether it was accepted, is still waiting,
        or lapsed — and where you cancel one, or send a fresh link to someone who never
        got theirs.
      </PageHeader>

      {head.error ? (
        <FailureState
          failure={classifyApiError(head.error)}
          onRetry={() => void head.refetch()}
          deniedWhat="reading invitations"
        />
      ) : head.isPending ? (
        <LoadingRegion label="Loading invitations.">
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        </LoadingRegion>
      ) : rows.length === 0 ? (
        <EmptyState title="No invitations yet">
          <p>Invite someone from the Employees section and their invitation appears here.</p>
        </EmptyState>
      ) : (
        <>
          <TableShell
            caption="Invitations with addressee, role, status, when they were sent and expire, and what you can do about each."
            headings={["Sent", "Addressee", "Role", "Status", "Expires", "Actions"]}
          >
            {rows.map((invitation) => (
              <tr key={invitation.id} className="border-b border-hairline last:border-b-0">
                <td className="px-5 py-3.5 text-xs text-muted-foreground">
                  <When iso={invitation.created_at} />
                </td>
                <td className="px-5 py-3.5 text-foreground">{invitation.email}</td>
                <td className="px-5 py-3.5 text-muted-foreground">
                  {ROLE_LABELS[invitation.role] ?? invitation.role}
                </td>
                <td className="px-5 py-3.5">
                  <InvitationStatus status={invitation.status} />
                </td>
                <td className="px-5 py-3.5 text-xs text-muted-foreground">
                  <When iso={invitation.expires_at} />
                </td>
                <td className="px-5 py-3.5">
                  {/* Only a waiting invitation has anything to cancel, and only an
                      unaccepted one can be reissued — an accepted invitation belongs to
                      a member now, and removing a member is deactivation, not this. */}
                  {invitation.status === "accepted" || invitation.status === "revoked" ? (
                    <span className="text-xs text-muted-foreground">—</span>
                  ) : (
                    <div className="flex flex-wrap gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={acting === invitation.id}
                        aria-busy={acting === invitation.id}
                        aria-label={`Resend the invitation to ${invitation.email}`}
                        onClick={() => void act(invitation, "resend")}
                        className="h-8 text-xs"
                      >
                        Resend
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        disabled={acting === invitation.id}
                        aria-busy={acting === invitation.id}
                        aria-label={`Cancel the invitation to ${invitation.email}`}
                        onClick={() => void act(invitation, "revoke")}
                        className="h-8 text-xs text-destructive hover:text-destructive"
                      >
                        Cancel
                      </Button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </TableShell>
          {more ? <LoadMore onClick={() => void loadOlder()} pending={loadingMore} /> : null}
        </>
      )}
    </div>
  );
}
