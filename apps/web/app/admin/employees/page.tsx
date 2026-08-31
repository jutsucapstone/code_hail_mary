"use client";

import { useCallback, useEffect, useState } from "react";

import { useCapabilities } from "@/components/admin/admin-shell";
import { ErrorState, LoadingRegion, PermissionDenied, Skeleton } from "@/components/states";
import { Field } from "@/components/pilot/field";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import { ApiError, api } from "@/lib/api";
import type { components } from "@/lib/api-schema";
import { ROLE_LABELS, can } from "@/lib/permissions";

type Employee = components["schemas"]["Employee"];
type Role = components["schemas"]["Role"];

/**
 * The people in an organisation, and the form that adds one.
 *
 * The invite form is only rendered for a caller holding `member:invite` — which hides a
 * control they cannot use, and is emphatically not the enforcement. `POST
 * /v1/employees/invitations` re-checks server-side, and it also refuses any role the
 * inviter does not outrank, so an HR Admin cannot invite an Owner even by crafting the
 * request by hand.
 *
 * Roles offered in the dropdown are filtered the same way and for the same reason: it is
 * a courtesy that keeps the form honest about what will succeed.
 */

/** Ranks mirror the seeded catalogue. Used only to decide what to offer.
 *
 * Total over `Role`, like the labels, so a role added to the API cannot quietly get no
 * rank — `undefined < actorRank` is false, and the new role would silently vanish from
 * the invite dropdown with nothing failing. */
const ROLE_RANKS: Record<Role, number> = {
  owner: 100,
  super_admin: 80,
  hr_admin: 60,
  it_admin: 60,
  analyst: 40,
  viewer: 20,
  member: 10,
};

function StatusPill({ status }: { status: string }) {
  // Status is never conveyed by colour alone: the word is the signal, and the tint only
  // reinforces it. A colour-only status is unreadable to a screen reader and to anyone
  // who cannot distinguish the hue.
  const tone =
    status === "active"
      ? "bg-brand/12 text-brand"
      : status === "invited"
        ? "bg-graph/12 text-graph"
        : "bg-muted text-muted-foreground";
  return (
    <span
      className={`inline-flex rounded-full px-2 py-0.5 font-mono text-[0.625rem] uppercase tracking-[0.16em] ${tone}`}
    >
      {status}
    </span>
  );
}

export default function EmployeesPage() {
  const capabilities = useCapabilities();
  const [page, setPage] = useState<{ items: Employee[]; next_cursor: string | null } | null>(
    null,
  );
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(
    null,
  );
  const [query, setQuery] = useState("");
  const [inviting, setInviting] = useState(false);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [invited, setInvited] = useState<string | null>(null);

  const mayRead = can(capabilities, "member:read");
  const mayInvite = can(capabilities, "member:invite");

  const load = useCallback(
    (search: string) => {
      if (!mayRead) return;
      api
        .employees({ q: search || null })
        .then((result) => {
          setPage(result);
          setFailure(null);
        })
        .catch((error: unknown) => {
          setFailure({
            message:
              error instanceof ApiError ? error.message : "We could not reach the service.",
            requestId: error instanceof ApiError ? error.requestId : undefined,
          });
        });
    },
    [mayRead],
  );

  useEffect(() => {
    load(query);
  }, [load, query]);

  async function onInvite(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setInviting(true);
    setInviteError(null);
    setInvited(null);

    // Captured before the await. React nulls `event.currentTarget` once the handler
    // yields, so touching it afterwards throws — and because that throw lands in the
    // catch below, a *successful* invitation reported itself as a failure. The user
    // would then re-send and hit a spurious "already has an invitation waiting".
    const element = event.currentTarget;
    const form = new FormData(element);
    const email = String(form.get("email") ?? "");

    try {
      await api.invite({ email, role: String(form.get("role") ?? "member") as Role });
      setInvited(email);
      element.reset();
      load(query);
    } catch (error) {
      setInviteError(
        error instanceof ApiError ? error.message : "We could not send that invitation.",
      );
    } finally {
      setInviting(false);
    }
  }

  if (!mayRead) {
    return <PermissionDenied what="permission to see the people in this organisation" />;
  }

  const actorRank = ROLE_RANKS[capabilities.role] ?? 0;
  // `Object.keys` is typed as `string[]` regardless of the record's key type — a
  // deliberate looseness in the standard library, since a value may carry extra keys at
  // runtime. This object is a literal declared above, so the assertion is sound, and it
  // is what lets the label lookup below stay exhaustive rather than falling back.
  const grantable = (Object.keys(ROLE_RANKS) as Role[]).filter(
    (role) => ROLE_RANKS[role] < actorRank,
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-10 [@media(max-height:820px)]:gap-6">
      <header>
        <p className="eyebrow flex items-center gap-2.5 text-brand">
          <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
          Employees
        </p>
        <h1 className="display mt-4 text-3xl font-semibold [@media(max-height:820px)]:mt-2 [@media(max-height:820px)]:text-2xl sm:text-4xl">
          People
        </h1>
        <p className="mt-3 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          Everyone in your organisation. Inviting someone issues their JUTSU ID when they
          accept — not before, so an unaccepted invitation never consumes one.
        </p>
      </header>

      {mayInvite ? (
        <section
          aria-labelledby="invite-heading"
          className="rounded-2xl border border-hairline bg-surface/40 p-6 [@media(max-height:820px)]:p-4 sm:p-7"
        >
          <h2 id="invite-heading" className="display text-lg font-semibold">
            Invite someone
          </h2>
          <form onSubmit={onInvite} className="mt-5 flex flex-col gap-4 sm:flex-row sm:items-end">
            <Field
              id="invite-email"
              name="email"
              type="email"
              label="Work email"
              required
              maxLength={320}
              className="flex-1"
            />
            <div className="flex flex-col gap-2 sm:w-56">
              <label htmlFor="invite-role" className="text-sm font-medium text-foreground">
                Role
              </label>
              <select
                id="invite-role"
                name="role"
                defaultValue="member"
                className="h-11 rounded-xl border border-hairline-strong bg-surface/40 px-3.5 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
              >
                {grantable.map((role) => (
                  <option key={role} value={role}>
                    {ROLE_LABELS[role] ?? role}
                  </option>
                ))}
              </select>
            </div>
            <SubmitButton
              pending={inviting}
              pendingLabel="Sending…"
              className="sm:w-40"
            >
              Send invitation
            </SubmitButton>
          </form>

          {inviteError ? (
            <div className="mt-4">
              <FormError message={inviteError} />
            </div>
          ) : null}

          {/* Announced, not just shown: the form resets on success, so a purely visual
              confirmation would leave a screen-reader user unsure anything happened. */}
          <p role="status" aria-live="polite" className="mt-4 min-h-5 text-sm text-muted-foreground">
            {invited ? `Invitation sent to ${invited}.` : ""}
          </p>
        </section>
      ) : null}

      <section aria-labelledby="people-heading" className="flex min-h-0 flex-1 flex-col gap-4">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <h2 id="people-heading" className="display text-xl font-semibold sm:text-2xl">
            All people
          </h2>
          <Field
            id="employee-search"
            name="q"
            label="Search"
            placeholder="Name or email"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="w-full sm:w-64"
          />
        </div>

        {failure ? (
          <ErrorState
            message={failure.message}
            requestId={failure.requestId}
            onRetry={() => load(query)}
          />
        ) : !page ? (
          <LoadingRegion label="Loading people.">
            <div className="flex flex-col gap-2">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-14" />
              ))}
            </div>
          </LoadingRegion>
        ) : page.items.length === 0 ? (
          <div className="rounded-2xl border border-hairline bg-surface/40 p-8 text-center">
            <p className="text-sm text-muted-foreground">
              {query
                ? "Nobody matches that search."
                : "Nobody has been invited yet. Everyone you invite appears here."}
            </p>
          </div>
        ) : (
          /* The TABLE scrolls, not the page.

             With two people this container is irrelevant; with fifty it is the whole
             point — page height would otherwise grow with headcount and no amount of
             spacing tuning would keep the invite form on screen. Bounding it here means
             the chrome stays put and only the rows move.

             `relative` is load-bearing: a static scroll box is not a containing block, so
             the table's min-width escapes and stretches the page sideways. */
          <div className="relative min-h-0 flex-1 overflow-auto rounded-2xl border border-hairline-strong">
            <table className="w-full min-w-[44rem] border-collapse text-sm">
              <caption className="sr-only">
                People in your organisation, with their JUTSU ID, role and status.
              </caption>
              <thead>
                {/* Sticky on each cell rather than on <thead>: a sticky thead is still
                    not honoured consistently, and the column headings must stay readable
                    once the rows start scrolling under them. The background is opaque so
                    rows do not show through. */}
                <tr className="text-left">
                  {["Person", "JUTSU ID", "Role", "Status"].map((heading) => (
                    <th
                      key={heading}
                      scope="col"
                      className="sticky top-0 z-10 border-b border-hairline bg-background px-5 py-3 font-medium text-muted-foreground"
                    >
                      {heading}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {page.items.map((person) => (
                  <tr key={person.id} className="border-b border-hairline last:border-b-0">
                    <th scope="row" className="px-5 py-4 text-left font-normal">
                      <span className="block text-foreground">
                        {person.display_name ?? "Not yet set"}
                      </span>
                      <span className="block text-xs text-muted-foreground">
                        {person.email}
                      </span>
                    </th>
                    <td className="px-5 py-4 font-mono text-xs text-muted-foreground">
                      {person.jutsu_id ?? "—"}
                    </td>
                    <td className="px-5 py-4 text-muted-foreground">
                      {person.role ? (ROLE_LABELS[person.role] ?? person.role) : "—"}
                    </td>
                    <td className="px-5 py-4">
                      <StatusPill status={person.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
