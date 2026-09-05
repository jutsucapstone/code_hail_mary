"use client";

import { useCallback, useEffect, useState } from "react";

import { Fragment } from "react";

import { useCapabilities } from "@/components/admin/admin-shell";
import { EmployeeConnections } from "@/components/admin/employee-connections";
import { RoleAssignment } from "@/components/admin/role-assignment";
import { LoadMore } from "@/components/admin/page-scaffold";
import { ErrorState, LoadingRegion, PermissionDenied, Skeleton } from "@/components/states";
import { Field } from "@/components/pilot/field";
import { FormError, SubmitButton } from "@/components/pilot/submit-button";
import { ApiError, api } from "@/lib/api";
import type { components } from "@/lib/api-schema";
import { toast } from "sonner";

import { classifyApiError } from "@/lib/api-error";
import { ROLE_LABELS, can } from "@/lib/permissions";

type Employee = components["schemas"]["Employee"];
type Role = components["schemas"]["Role"];
type Catalogue = components["schemas"]["Catalogue"];

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
  // Older pages accumulate under the head page as the reader walks back. `exhausted`
  // is distinct from `cursor === null`, which is also the state before any walk:
  // without it the null cursor falls back to the head page's cursor and the walk
  // restarts, re-appending page two under a resurrected button.
  const [older, setOlder] = useState<Employee[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [exhausted, setExhausted] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [inviting, setInviting] = useState(false);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [invited, setInvited] = useState<string | null>(null);

  const mayRead = can(capabilities, "member:read");
  const mayInvite = can(capabilities, "member:invite");
  const mayAssign = can(capabilities, "member:assign_role");
  const mayReadConnections = can(capabilities, "integration:read");
  const [openConnections, setOpenConnections] = useState<string | null>(null);
  const [changingRole, setChangingRole] = useState<string | null>(null);
  const mayAssignTaxonomy = can(capabilities, "member:assign_role_code");
  // Only an Owner or Super Admin may seat somebody in CHM/CEO/ITA/HRA. Mirrored from
  // the server's rule so the dropdown does not offer what the API would refuse; the
  // API enforces it regardless of what this computes.
  const canSeatGovernance =
    capabilities.role === "owner" || capabilities.role === "super_admin";
  const [openRole, setOpenRole] = useState<string | null>(null);
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [practice, setPractice] = useState("");
  const [level, setLevel] = useState("");
  const [onlyUnmapped, setOnlyUnmapped] = useState(false);

  const load = useCallback(
    (search: string) => {
      if (!mayRead) return;
      api
        .employees({
          q: search || null,
          practice: practice || null,
          level: level || null,
          unmapped: onlyUnmapped,
        })
        .then((result) => {
          setPage(result);
          // A fresh head page starts a fresh walk; stale older pages belong to the
          // previous search.
          setOlder([]);
          setCursor(null);
          setExhausted(false);
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
    [mayRead, practice, level, onlyUnmapped],
  );

  useEffect(() => {
    load(query);
  }, [load, query]);

  // Global reference data, identical for every organisation and unchanged between
  // deploys, so it is fetched once for the page rather than per row.
  useEffect(() => {
    if (!mayRead) return;
    let cancelled = false;
    api
      .roleCatalogue()
      .then((result) => {
        if (!cancelled) setCatalogue(result);
      })
      .catch(() => {
        // A missing catalogue disables the editor and leaves the roster readable. It is
        // reference data for a control, not the page's reason to exist.
        if (!cancelled) setCatalogue(null);
      });
    return () => {
      cancelled = true;
    };
  }, [mayRead]);

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
      const roleTitle = String(form.get("role_title") ?? "").trim();
      await api.invite({
        email,
        role: String(form.get("role") ?? "member") as Role,
        role_title: roleTitle || null,
      });
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

  async function loadOlder() {
    const next = cursor ?? page?.next_cursor;
    if (!next) return;
    setLoadingMore(true);
    try {
      const result = await api.employees({
        q: query || null,
        cursor: next,
        practice: practice || null,
        level: level || null,
        unmapped: onlyUnmapped,
      });
      setOlder((current) => [...current, ...result.items]);
      setCursor(result.next_cursor);
      if (result.next_cursor === null) setExhausted(true);
    } catch (error) {
      toast.error(classifyApiError(error).message);
    } finally {
      setLoadingMore(false);
    }
  }

  async function onChangeRole(person: Employee, role: Role) {
    setChangingRole(person.id);
    try {
      await api.assignRole(person.id, { role });
      toast.success(`${person.display_name ?? person.email} is now ${ROLE_LABELS[role]}.`);
      load(query);
    } catch (error) {
      // The server's refusals are the interesting ones — a peer, a rank above yours,
      // yourself — and its message says which. Surface it verbatim.
      toast.error(classifyApiError(error).message);
    } finally {
      setChangingRole(null);
    }
  }

  if (!mayRead) {
    return <PermissionDenied what="permission to see the people in this organisation" />;
  }

  // Derived rather than written twice: the expanding panels span the whole row, and a
  // column added above without updating a hardcoded number leaves them visibly short.
  const COLUMN_COUNT =
    7 + (mayReadConnections ? 1 : 0) + (mayAssign ? 1 : 0) + (mayAssignTaxonomy ? 1 : 0);

  const actorRank = ROLE_RANKS[capabilities.role] ?? 0;
  // `Object.keys` is typed as `string[]` regardless of the record's key type — a
  // deliberate looseness in the standard library, since a value may carry extra keys at
  // runtime. This object is a literal declared above, so the assertion is sound, and it
  // is what lets the label lookup below stay exhaustive rather than falling back.
  const grantable = (Object.keys(ROLE_RANKS) as Role[]).filter(
    (role) => ROLE_RANKS[role] < actorRank,
  );

  const rows = page ? [...page.items, ...older] : [];
  const more = !exhausted && (cursor ?? page?.next_cursor);

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
            <Field
              id="invite-role-title"
              name="role_title"
              label="Role title (optional)"
              placeholder="e.g. Head of Platform"
              maxLength={128}
              className="sm:w-64"
            />
            <SubmitButton
              pending={inviting}
              pendingLabel="Sending…"
              className="sm:w-40"
            >
              Send invitation
            </SubmitButton>
          </form>
          <p className="mt-2 text-xs text-muted-foreground">
            A title is how the role reads on their profile. What they may do comes from
            the role you selected — a title grants nothing.
          </p>

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

          {/* The Expert Finder controls. `level` matches NORMALIZED seniority, so it
              finds the Senior Software Engineer, the Audit Senior and the Senior Tax
              Consultant together — people whose titles share no word. */}
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">Practice</span>
            <select
              value={practice}
              onChange={(event) => setPractice(event.target.value)}
              className="h-11 rounded-xl border border-hairline-strong bg-surface/40 px-3.5 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand sm:w-52"
            >
              <option value="">Every practice</option>
              {(catalogue?.practices ?? []).map((entry) => (
                <option key={entry.key} value={entry.key}>
                  {entry.display_name}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">Seniority</span>
            <select
              value={level}
              onChange={(event) => setLevel(event.target.value)}
              className="h-11 rounded-xl border border-hairline-strong bg-surface/40 px-3.5 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand sm:w-52"
            >
              <option value="">Every level</option>
              {(catalogue?.levels ?? []).map((entry) => (
                <option key={entry.key} value={entry.key}>
                  {entry.display_name}
                </option>
              ))}
            </select>
          </label>

          <label className="flex items-center gap-2 pb-2.5 text-sm text-muted-foreground sm:self-end">
            <input
              type="checkbox"
              checked={onlyUnmapped}
              onChange={(event) => setOnlyUnmapped(event.target.checked)}
              className="size-4 rounded border-hairline-strong"
            />
            Needs mapping
          </label>
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
             the table's min-width escapes and stretches the page sideways.

             The height floor is load-bearing too. min-h-0 let this box collapse to
             nineteen pixels on a short viewport, and the two expanding panels below
             render INSIDE it — a 306px role editor inside a 19px scroller is not a
             cramped screen, it is an unusable one. With a floor the box stops
             shrinking and the main element scrolls instead. */
          <>
          <div className="relative min-h-[22rem] flex-1 overflow-auto rounded-2xl border border-hairline-strong">
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
                  {[
                    "Person",
                    "JUTSU ID",
                    "Role",
                    "Role title",
                    "Seniority",
                    "Code",
                    "Status",
                    ...(mayReadConnections ? ["Integrations"] : []),
                    ...(mayAssign ? ["Change role"] : []),
                    ...(mayAssignTaxonomy ? ["Role info"] : []),
                  ].map((heading) => (
                    <th
                      key={heading}
                      scope="col"
                      className="sticky top-0 z-10 border-b border-hairline bg-background px-3 py-3 font-medium text-muted-foreground"
                    >
                      {heading}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((person) => (
                  <Fragment key={person.id}>
                  <tr className="border-b border-hairline last:border-b-0">
                    <th scope="row" className="px-3 py-4 text-left font-normal">
                      <span className="block text-foreground">
                        {person.display_name ?? "Not yet set"}
                      </span>
                      <span className="block text-xs text-muted-foreground">
                        {person.email}
                      </span>
                    </th>
                    <td className="px-3 py-4 font-mono text-xs text-muted-foreground">
                      {person.jutsu_id ?? "—"}
                    </td>
                    <td className="px-3 py-4 text-muted-foreground">
                      {person.role ? (ROLE_LABELS[person.role] ?? person.role) : "—"}
                    </td>
                    {/* The ACTUAL title, in the practice's own vocabulary. Shown beside
                        the normalized level rather than replaced by it: comparing people
                        must not rename them. */}
                    <td className="px-3 py-4 text-muted-foreground">
                      {person.role_title ?? "—"}
                      {person.practice ? (
                        <span className="block text-xs text-muted-foreground/70">
                          {person.practice}
                        </span>
                      ) : null}
                    </td>
                    <td className="px-3 py-4 text-muted-foreground">
                      {person.role_level ?? (
                        <span className="text-xs uppercase tracking-[0.14em] text-muted-foreground/70">
                          Needs mapping
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-4 font-mono text-xs text-muted-foreground">
                      {person.role_code ?? "—"}
                    </td>
                    <td className="px-3 py-4">
                      <StatusPill status={person.status} />
                    </td>
                    {mayReadConnections ? (
                      <td className="px-3 py-4">
                        <button
                          type="button"
                          aria-expanded={openConnections === person.id}
                          aria-controls={`employee-connections-${person.id}`}
                          onClick={() =>
                            setOpenConnections((current) =>
                              current === person.id ? null : person.id,
                            )
                          }
                          className="rounded-lg border border-hairline-strong px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:border-brand/40 hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                        >
                          {openConnections === person.id ? "Hide" : "View"}
                          {/* Named like the role control above: a column of bare
                              "View" buttons is unreadable off a screen-reader rotor. */}
                          <span className="sr-only">
                            {" "}
                            integrations for {person.display_name ?? person.email}
                          </span>
                        </button>
                      </td>
                    ) : null}
                    {mayAssign ? (
                      <td className="px-3 py-4">
                        {person.id === capabilities.user_id ? (
                          /* The server refuses self-changes even for the owner; offering
                             the control would teach people to click a button that cannot
                             work. */
                          <span className="text-xs text-muted-foreground">You</span>
                        ) : person.role && (ROLE_RANKS[person.role] ?? 0) >= actorRank ? (
                          /* At or above the actor's rank: the server will refuse, so the
                             control says why instead of offering a doomed dropdown. */
                          <span className="text-xs text-muted-foreground">Outranks you</span>
                        ) : (
                          <label className="flex items-center gap-2">
                            <span className="sr-only">
                              Change role for {person.display_name ?? person.email}
                            </span>
                            <select
                              value={person.role ?? ""}
                              disabled={changingRole === person.id || !person.role}
                              onChange={(event) =>
                                void onChangeRole(person, event.target.value as Role)
                              }
                              className="h-9 rounded-lg border border-hairline-strong bg-surface/40 px-2.5 text-xs text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
                            >
                              {person.role && !grantable.includes(person.role) ? (
                                <option value={person.role} disabled>
                                  {ROLE_LABELS[person.role]}
                                </option>
                              ) : null}
                              {grantable.map((role) => (
                                <option key={role} value={role}>
                                  {ROLE_LABELS[role]}
                                </option>
                              ))}
                            </select>
                          </label>
                        )}
                      </td>
                    ) : null}
                    {mayAssignTaxonomy ? (
                      <td className="px-3 py-4">
                        <button
                          type="button"
                          aria-expanded={openRole === person.id}
                          aria-controls={`employee-role-${person.id}`}
                          onClick={() =>
                            setOpenRole((open) => (open === person.id ? null : person.id))
                          }
                          className="rounded-lg border border-hairline-strong px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:border-brand/40 hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                        >
                          {openRole === person.id ? "Hide" : "Edit"}
                          {/* A column of bare "Edit" buttons is unreadable off a
                              screen-reader rotor. */}
                          <span className="sr-only">
                            {" "}
                            role information for {person.display_name ?? person.email}
                          </span>
                        </button>
                      </td>
                    ) : null}
                  </tr>
                  {mayAssignTaxonomy && openRole === person.id ? (
                    <tr
                      id={`employee-role-${person.id}`}
                      className="border-b border-hairline last:border-b-0"
                    >
                      <td colSpan={COLUMN_COUNT} className="bg-surface/30 px-5 py-4">
                        <RoleAssignment
                          userId={person.id}
                          personName={person.display_name ?? person.email}
                          catalogue={catalogue}
                          canSeatGovernance={canSeatGovernance}
                          isSelf={person.id === capabilities.user_id}
                          onSaved={() => {
                            setOpenRole(null);
                            load(query);
                          }}
                        />
                      </td>
                    </tr>
                  ) : null}
                  {mayReadConnections && openConnections === person.id ? (
                    <tr
                      id={`employee-connections-${person.id}`}
                      className="border-b border-hairline last:border-b-0"
                    >
                      <td colSpan={COLUMN_COUNT} className="bg-surface/30 px-5 py-4">
                        <EmployeeConnections
                          userId={person.id}
                          mayRevoke={can(capabilities, "integration:revoke")}
                        />
                      </td>
                    </tr>
                  ) : null}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
          {more ? <LoadMore onClick={() => void loadOlder()} pending={loadingMore} /> : null}
          </>
        )}
      </section>
    </div>
  );
}
