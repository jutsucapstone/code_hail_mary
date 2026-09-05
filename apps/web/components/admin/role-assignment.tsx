"use client";

import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { ErrorState, LoadingRegion, Skeleton } from "@/components/states";
import { classifyApiError } from "@/lib/api-error";
import { ApiError, api } from "@/lib/api";
import type { components } from "@/lib/api-schema";

type Catalogue = components["schemas"]["Catalogue"];
type Taxonomy = components["schemas"]["Taxonomy"];

const SELECT_CLASS =
  "h-11 w-full rounded-xl border border-hairline-strong bg-surface/40 px-3.5 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60";

/**
 * Assign one person's practice, role title, normalized level and platform role code.
 *
 * The three selects are chained because the catalogue is: choosing a practice narrows the
 * titles, and choosing a title narrows the levels to exactly the ones the source document
 * admits for it. That is a courtesy, not the enforcement — `PATCH
 * /v1/employees/{id}/role-assignment` re-derives all of it, and the database refuses an
 * impossible pairing through a composite foreign key even if the API were bypassed.
 *
 * **Ambiguity is shown, not resolved.** Six titles map to two levels in the source
 * ("Audit Senior → Consultant / Senior Consultant"), so the level select offers both and
 * the reader is told the choice is theirs. Pre-selecting one silently would make this
 * screen assert something the source document does not.
 *
 * A governance code (CHM/CEO/ITA/HRA) is offered only to a caller the server would
 * actually accept it from — Owner or Super Admin — and never for oneself. Hiding it is
 * again a courtesy: the server enforces both rules and says so in its refusal.
 */
export function RoleAssignment({
  userId,
  personName,
  catalogue,
  canSeatGovernance,
  isSelf,
  onSaved,
}: {
  userId: string;
  personName: string;
  catalogue: Catalogue | null;
  /** Whether the CALLER may seat somebody in one of the four governance codes. */
  canSeatGovernance: boolean;
  isSelf: boolean;
  onSaved: () => void;
}) {
  const [current, setCurrent] = useState<Taxonomy | null>(null);
  const [failure, setFailure] = useState<{ message: string; requestId?: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [practice, setPractice] = useState("");
  const [titleKey, setTitleKey] = useState("");
  const [customTitle, setCustomTitle] = useState("");
  const [level, setLevel] = useState("");
  const [code, setCode] = useState("");
  const [useCustom, setUseCustom] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .roleAssignment(userId)
      .then((assignment) => {
        if (cancelled) return;
        setCurrent(assignment);
        setPractice(assignment.practice_key ?? "");
        setTitleKey(assignment.role_title_key ?? "");
        setLevel(assignment.role_level_key ?? "");
        setCode(assignment.role_code ?? "");
        setUseCustom(assignment.mapping_status === "custom");
        setCustomTitle(assignment.mapping_status === "custom" ? (assignment.role_title ?? "") : "");
        setFailure(null);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        // A person with no assignment yet is the ordinary case, not a fault: the API
        // says 404 and this screen exists precisely to fill it in.
        if (cause instanceof ApiError && cause.status === 404) {
          setCurrent(null);
          setFailure(null);
          return;
        }
        setFailure(classifyApiError(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const titles = useMemo(
    () => (catalogue?.titles ?? []).filter((title) => title.practice_key === practice),
    [catalogue, practice],
  );

  const selectedTitle = useMemo(
    () => titles.find((title) => title.key === titleKey) ?? null,
    [titles, titleKey],
  );

  /** Exactly the levels the source admits for this title — or all of them on the custom
   *  path, where no catalogue title constrains the choice. */
  const levels = useMemo(() => {
    const all = catalogue?.levels ?? [];
    if (useCustom || !selectedTitle) return all;
    return all.filter((entry) => selectedTitle.level_keys.includes(entry.key));
  }, [catalogue, selectedTitle, useCustom]);

  const codes = useMemo(
    () =>
      (catalogue?.codes ?? []).filter(
        (entry) => !entry.privileged || (canSeatGovernance && !isSelf),
      ),
    [catalogue, canSeatGovernance, isSelf],
  );

  const ambiguous = !useCustom && (selectedTitle?.level_keys.length ?? 0) > 1;

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await api.assignRoleTaxonomy(userId, {
        practice_key: useCustom ? null : practice || null,
        role_title_key: useCustom ? null : titleKey || null,
        role_title_custom: useCustom ? customTitle.trim() || null : null,
        role_level_key: level || null,
        role_code: code || null,
      });
      toast.success(`Updated the role information for ${personName}.`);
      onSaved();
    } catch (cause) {
      // The server's refusals are the interesting ones — a wrong practice, a level the
      // catalogue does not admit, a governance seat above the caller's rank — and its
      // message names which. Surface it verbatim rather than paraphrasing.
      setError(classifyApiError(cause).message);
    } finally {
      setSaving(false);
    }
  }

  if (failure) {
    return <ErrorState message={failure.message} requestId={failure.requestId} />;
  }

  if (!catalogue) {
    return (
      <LoadingRegion label="Loading the role catalogue.">
        <Skeleton className="h-40" />
      </LoadingRegion>
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="role-assignment">
      <p className="text-xs text-muted-foreground">
        The <span className="text-foreground">role title</span> is what this person is
        called in their practice. The{" "}
        <span className="text-foreground">normalized level</span> is what makes that
        comparable with every other practice, and the{" "}
        <span className="text-foreground">role code</span> is where they sit on the org
        chart. None of the three changes what anyone is permitted to do.
      </p>

      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">Practice</span>
          <select
            className={SELECT_CLASS}
            value={useCustom ? "" : practice}
            disabled={useCustom}
            onChange={(event) => {
              setPractice(event.target.value);
              setTitleKey("");
              setLevel("");
            }}
          >
            <option value="">Not set</option>
            {catalogue.practices.map((entry) => (
              <option key={entry.key} value={entry.key}>
                {entry.display_name}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">Role title</span>
          {useCustom ? (
            <input
              className={SELECT_CLASS}
              value={customTitle}
              maxLength={128}
              placeholder="Your organisation's own title"
              onChange={(event) => setCustomTitle(event.target.value)}
            />
          ) : (
            <select
              className={SELECT_CLASS}
              value={titleKey}
              disabled={!practice}
              onChange={(event) => {
                const next = event.target.value;
                setTitleKey(next);
                // Pre-select only where the source is unambiguous. Where it offers two,
                // leave it empty so the choice is visibly the administrator's.
                const chosen = titles.find((title) => title.key === next);
                setLevel(
                  chosen && chosen.level_keys.length === 1 ? chosen.default_level_key : "",
                );
              }}
            >
              <option value="">{practice ? "Not set" : "Choose a practice first"}</option>
              {titles.map((title) => (
                <option key={title.key} value={title.key}>
                  {title.display_name}
                </option>
              ))}
            </select>
          )}
        </label>

        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">Normalized level</span>
          <select
            className={SELECT_CLASS}
            value={level}
            onChange={(event) => setLevel(event.target.value)}
          >
            <option value="">Not set</option>
            {levels.map((entry) => (
              <option key={entry.key} value={entry.key}>
                {entry.display_name}
              </option>
            ))}
          </select>
          {ambiguous ? (
            <span className="text-xs text-muted-foreground">
              The taxonomy lists two levels for this title. Choose the one that applies.
            </span>
          ) : null}
        </label>

        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">Platform role code</span>
          <select
            className={SELECT_CLASS}
            value={code}
            onChange={(event) => setCode(event.target.value)}
          >
            <option value="">Not set</option>
            {codes.map((entry) => (
              <option key={entry.code} value={entry.code}>
                {entry.code} — {entry.display_name} (T{entry.tier})
              </option>
            ))}
          </select>
          {!canSeatGovernance || isSelf ? (
            <span className="text-xs text-muted-foreground">
              {isSelf
                ? "Governance codes cannot be assigned to yourself."
                : "Governance codes are assigned by an Owner or Super Admin."}
            </span>
          ) : null}
        </label>
      </div>

      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        <input
          type="checkbox"
          checked={useCustom}
          onChange={(event) => {
            setUseCustom(event.target.checked);
            setTitleKey("");
            setPractice("");
          }}
          className="size-4 rounded border-hairline-strong"
        />
        This organisation uses a title the catalogue does not carry
      </label>

      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => void save()}
          disabled={saving}
          className="h-10 rounded-xl bg-brand px-4 text-sm font-semibold text-brand-foreground transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
        >
          {saving ? "Saving…" : "Save role information"}
        </button>
        {current ? (
          <span className="text-xs text-muted-foreground">
            Currently {current.role_title ?? "unassigned"}
            {current.role_level ? ` · ${current.role_level}` : ""}
            {current.role_code ? ` · ${current.role_code}` : ""}
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">No role information yet.</span>
        )}
      </div>
    </div>
  );
}
