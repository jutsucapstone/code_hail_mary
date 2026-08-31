"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

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
 * Invitations — what happened to every one this organisation sent.
 *
 * The addressee's email is shown deliberately: the caller holds `member:invite`, and an
 * invitation *is* an email address. Status is derived server-side against the database
 * clock, so "expired" here and "expired" at acceptance time cannot disagree.
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
  const [loadingMore, setLoadingMore] = useState(false);

  const mayRead = can(capabilities, "member:invite");

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
    } finally {
      setLoadingMore(false);
    }
  }

  const rows = [...(head.data?.items ?? []), ...older];
  const more = cursor ?? head.data?.next_cursor;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-8 [@media(max-height:820px)]:gap-6">
      <PageHeader eyebrow="People" title="Invitations">
        Every invitation this organisation has sent, newest first. Sending one is done
        from Employees; this is where you see whether it was accepted, is still waiting,
        or lapsed.
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
            caption="Invitations with addressee, role, status, and when they were sent and expire."
            headings={["Sent", "Addressee", "Role", "Status", "Expires"]}
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
              </tr>
            ))}
          </TableShell>
          {more ? <LoadMore onClick={() => void loadOlder()} pending={loadingMore} /> : null}
        </>
      )}
    </div>
  );
}
