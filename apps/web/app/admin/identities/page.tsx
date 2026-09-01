"use client";

import { useCallback, useEffect, useState } from "react";

import { useCapabilities } from "@/components/admin/admin-shell";
import { SourceIdentities } from "@/components/admin/source-identities";
import { ErrorState, LoadingRegion, PermissionDenied, Skeleton } from "@/components/states";
import { Field } from "@/components/pilot/field";
import { ApiError, api } from "@/lib/api";
import type { components } from "@/lib/api-schema";
import { ROLE_LABELS, can } from "@/lib/permissions";

type Employee = components["schemas"]["Employee"];

/**
 * Who each person is known as, and what that lets them read.
 *
 * This is the first UI over `GET/POST/DELETE /v1/employees/{user_id}/identities`, which
 * has been implemented and reachable since migration 0008 with nothing in the browser
 * calling it. The employees table was the only way to see people; there was no way at all
 * to see — or change — the identities that decide what those people can retrieve.
 *
 * Deliberately **not** called "Integrations". Linking a source identity grants document
 * access; connecting an application fetches content. The second capability does not exist
 * in this repository, and conflating the names is how a "Disconnect" button ends up
 * silently revoking a colleague's access to documents.
 */
export default function IdentitiesPage() {
  const capabilities = useCapabilities();
  const [employees, setEmployees] = useState<Employee[] | null>(null);
  const [selected, setSelected] = useState<Employee | null>(null);
  const [query, setQuery] = useState("");
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(null);

  const mayRead = can(capabilities, "member:read");

  // State is set inside `.then`/`.catch`, never synchronously in the effect below:
  // a synchronous setState there triggers a cascading render, which is what
  // `react-hooks/set-state-in-effect` is about. Same shape the employees page uses.
  // Search is server-side for the same reason it is on the employees page: one fetched
  // page of 25 is not the organisation, and a client-side filter over it would say
  // "nobody" while the person sat on page two.
  const load = useCallback((search: string) => {
    api
      .employees({ q: search || null })
      .then((page) => {
        setEmployees(page.items);
        setSelected((current) => current ?? page.items[0] ?? null);
        setFailure(null);
      })
      .catch((error: unknown) => {
        setEmployees([]);
        setFailure({
          message: error instanceof ApiError ? error.message : "That did not load.",
          requestId: error instanceof ApiError ? error.requestId : undefined,
        });
      });
  }, []);

  useEffect(() => {
    if (mayRead) load(query);
  }, [load, mayRead, query]);

  if (!mayRead) return <PermissionDenied what="the people in this organisation" />;

  return (
    <div>
      <h1 className="display text-2xl font-semibold sm:text-3xl">Source identities</h1>
      <p className="mt-3 max-w-prose text-pretty text-base leading-relaxed text-muted-foreground">
        Document permissions are granted to provider accounts, not to JUTSU logins. This is
        where you say which accounts a person is known by — and therefore which documents
        they can retrieve.
      </p>

      {/* Outside the branches below on purpose: a search that matches nobody must
          leave the field on screen, or there is no way to clear it. */}
      <div className="mt-8">
        <Field
          id="identity-search"
          name="q"
          label="Search"
          placeholder="Name or email"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          className="w-full sm:w-64"
        />
      </div>

      {employees === null ? (
        <LoadingRegion label="Loading people">
          <div className="mt-8 space-y-2">
            {[0, 1, 2].map((n) => (
              <Skeleton key={n} className="h-10 w-full" />
            ))}
          </div>
        </LoadingRegion>
      ) : failure ? (
        <div className="mt-8">
          <ErrorState
            message={failure.message}
            requestId={failure.requestId}
            onRetry={() => load(query)}
          />
        </div>
      ) : employees.length === 0 ? (
        <div className="mt-8 rounded-2xl border border-hairline bg-surface/40 p-6">
          <p className="eyebrow text-muted-foreground/80">Nobody to show</p>
          <p className="mt-3 text-pretty text-sm leading-relaxed text-muted-foreground">
            {query
              ? "Nobody matches that search."
              : "Invite someone from the Employees section first."}
          </p>
        </div>
      ) : (
        <>
          <div className="mt-8">
            <label
              htmlFor="identity-employee"
              className="eyebrow block text-muted-foreground/80"
            >
              Person
            </label>
            <select
              id="identity-employee"
              value={selected?.id ?? ""}
              onChange={(event) =>
                setSelected(employees.find((e) => e.id === event.target.value) ?? null)
              }
              className="mt-2 w-full max-w-md rounded-xl border border-hairline bg-background px-3 py-2.5 text-sm focus-visible:border-brand/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
            >
              {employees.map((employee) => (
                <option key={employee.id} value={employee.id}>
                  {employee.display_name ?? employee.email}
                  {employee.role ? ` · ${ROLE_LABELS[employee.role]}` : ""}
                </option>
              ))}
            </select>
          </div>

          {selected ? (
            <SourceIdentities employee={selected} capabilities={capabilities} />
          ) : null}
        </>
      )}
    </div>
  );
}
