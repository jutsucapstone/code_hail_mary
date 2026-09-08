"use client";

import { useCallback, useId, useRef, useState } from "react";
import { Loader2, Paperclip, Upload, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { FormError } from "@/components/pilot/submit-button";
import { api } from "@/lib/api";
import type { BulkPreview, BulkRowResult } from "@/lib/api";
import { classifyApiError } from "@/lib/api-error";
import type { components } from "@/lib/api-schema";
import { ROLE_LABELS } from "@/lib/permissions";
import { cn } from "@/lib/utils";

type Role = components["schemas"]["Role"];
type Outcome = BulkRowResult["outcome"];

/**
 * Adding a team, in three steps that stay on one screen: paste or drop a file, read the
 * preview, send.
 *
 * **The preview is the feature.** An administrator pasting last quarter's roster has six
 * people who already have accounts and two misspelt addresses in it, and the difference
 * between a bulk tool and a bulk mistake is whether they see those before seventy-two
 * other people receive mail. `POST …/preview` writes nothing and sends nothing; the
 * second request is the decision.
 *
 * **Nothing here is authorization.** The panel is only rendered for a caller holding
 * `member:invite`, the role dropdown offers only what they outrank, and both are
 * courtesies that keep the form honest about what will succeed. The server re-checks the
 * permission and re-checks the rank ceiling per row on the send — so a client that
 * skipped the preview, or edited its result, meets exactly the same refusals.
 */

/** How each outcome reads, and what the reader should do about it. */
const OUTCOMES: Record<Outcome, { label: string; tone: string; counts: boolean }> = {
  // `counts` marks the rows that will actually be acted on, so the button's number and
  // the work it does come from the same place.
  ready: { label: "Will invite", tone: "bg-brand/12 text-brand", counts: true },
  sent: { label: "Invited", tone: "bg-brand/12 text-brand", counts: false },
  already_member: {
    label: "Already here",
    tone: "bg-muted text-muted-foreground",
    counts: false,
  },
  already_invited: {
    label: "Already invited",
    tone: "bg-muted text-muted-foreground",
    counts: false,
  },
  duplicate: { label: "Repeated", tone: "bg-muted text-muted-foreground", counts: false },
  invalid_email: { label: "Not an address", tone: "bg-destructive/12 text-destructive", counts: false },
  invalid_role: { label: "Unknown role", tone: "bg-destructive/12 text-destructive", counts: false },
  role_too_high: { label: "Above your level", tone: "bg-destructive/12 text-destructive", counts: false },
  failed: { label: "Failed", tone: "bg-destructive/12 text-destructive", counts: false },
};

/** Mirrors `MAX_XLSX_BYTES` in `jutsu_api.bulk_invitations`. Refused before upload. */
const MAX_FILE_BYTES = 1_000_000;

/**
 * The outcomes a press of the button can still act on.
 *
 * `failed` belongs here as much as `ready` does: a row whose invitation could not be
 * emailed had its invitation revoked server-side precisely so the address is free to try
 * again, and "retry the failures" is the reason anybody looks at this table twice. While
 * this matched `ready` alone, the caption promised a retry the button could not perform.
 */
const ACTIONABLE: ReadonlySet<Outcome> = new Set<Outcome>(["ready", "failed"]);

/**
 * What the file picker accepts, and what the drop zone checks.
 *
 * Extensions rather than MIME types, because a browser reports `.csv` as
 * `application/vnd.ms-excel`, `text/csv` or `""` depending on what is installed. This is
 * a courtesy in any case — the server decides what it can parse, and it decides from the
 * bytes rather than from the name.
 */
const ACCEPTED = [".csv", ".tsv", ".txt", ".xlsx"];

function OutcomePill({ outcome }: { outcome: Outcome }) {
  // The word carries the meaning and the tint only reinforces it: a status conveyed by
  // colour alone is unreadable to a screen reader and to anyone who cannot tell the hues
  // apart.
  const shown = OUTCOMES[outcome] ?? {
    label: outcome,
    tone: "bg-muted text-muted-foreground",
  };
  return (
    <span
      className={cn(
        "inline-flex whitespace-nowrap rounded-full px-2 py-0.5 font-mono text-[0.625rem] uppercase tracking-[0.16em]",
        shown.tone,
      )}
    >
      {shown.label}
    </span>
  );
}

async function readAsBase64(file: File): Promise<string> {
  const buffer = new Uint8Array(await file.arrayBuffer());
  // Chunked, because spreading a megabyte into `String.fromCharCode` overflows the
  // argument limit and throws a RangeError that reads like a corrupt file.
  let binary = "";
  for (let index = 0; index < buffer.length; index += 8192) {
    binary += String.fromCharCode(...buffer.subarray(index, index + 8192));
  }
  return btoa(binary);
}

export function BulkOnboarding({
  grantable,
  onInvited,
}: {
  /** The roles this administrator outranks. The server refuses anything else. */
  grantable: Role[];
  /** Called after a send so the roster behind this panel can refresh. */
  onInvited: () => void;
}) {
  const headingId = useId();
  const pastedId = useId();
  const roleId = useId();
  const fileId = useId();

  const [pasted, setPasted] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [defaultRole, setDefaultRole] = useState<Role>(
    grantable.includes("member") ? "member" : (grantable[0] ?? "member"),
  );
  const [dragging, setDragging] = useState(false);
  const [checking, setChecking] = useState(false);
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [preview, setPreview] = useState<BulkPreview | null>(null);
  const [sent, setSent] = useState<{ sent: number; failed: number } | null>(null);
  /** Rows the reader has unticked. Keyed by position, since an address may repeat. */
  const [excluded, setExcluded] = useState<Set<number>>(new Set());
  /** Roles the reader overrode in the preview, by position. */
  const [overrides, setOverrides] = useState<Record<number, Role>>({});
  const fileInput = useRef<HTMLInputElement>(null);

  const reset = useCallback(() => {
    setPreview(null);
    setSent(null);
    setExcluded(new Set());
    setOverrides({});
  }, []);

  function chooseFile(next: File | null) {
    setFailure(null);
    reset();
    if (!next) {
      setFile(null);
      return;
    }
    const name = next.name.toLowerCase();
    if (!ACCEPTED.some((extension) => name.endsWith(extension))) {
      setFailure(`${next.name} is not a CSV or Excel file.`);
      return;
    }
    if (next.size > MAX_FILE_BYTES) {
      // Refused here as well as on the server, so a person who picked the wrong file
      // finds out immediately instead of after a megabyte of upload.
      setFailure(`${next.name} is too large. Export just the addresses and try again.`);
      return;
    }
    setFile(next);
    setPasted("");
  }

  async function check() {
    setChecking(true);
    setFailure(null);
    reset();
    try {
      let source: Parameters<typeof api.previewInvitations>[0];
      if (file) {
        const name = file.name.toLowerCase();
        source = name.endsWith(".xlsx")
          ? { xlsx_base64: await readAsBase64(file), role: defaultRole }
          : // A CSV is text and the browser already has it, so there is no upload and no
            // multipart handler — `File.text()` is the whole of it.
            { csv: await file.text(), role: defaultRole };
      } else {
        source = { emails: pasted, role: defaultRole };
      }
      setPreview(await api.previewInvitations(source));
    } catch (error) {
      setFailure(classifyApiError(error).message);
    } finally {
      setChecking(false);
    }
  }

  /** The rows a press of the button would act on: actionable, and still ticked. */
  function selected(from: BulkPreview): { row: BulkRowResult; index: number }[] {
    return from.rows
      .map((row, index) => ({ row, index }))
      .filter(({ row, index }) => ACTIONABLE.has(row.outcome) && !excluded.has(index));
  }

  async function send() {
    if (!preview) return;
    const rows = selected(preview);
    if (rows.length === 0) return;
    setSending(true);
    setFailure(null);
    try {
      const result = await api.inviteMany({
        rows: rows.map(({ row, index }) => ({
          email: row.email,
          role: overrides[index] ?? row.role,
          role_title: row.role_title,
        })),
      });
      // The result replaces the preview in place, so the same table the reader approved
      // is the one that reports back — every row keeps its position and its address.
      setPreview({ ...preview, rows: mergeResults(preview.rows, rows, result.rows) });
      setSent({ sent: result.sent, failed: result.failed });
      // **`excluded` is NOT cleared, and that is the whole fix.**
      //
      // `mergeResults` only overwrites the rows that were actually requested, so a row
      // the administrator unticked still reads `ready` afterwards. Clearing the exclusions
      // therefore re-selected exactly the people they had deliberately removed — and with
      // the tick column gone there was no way to remove them again. The button came back
      // reading "Send 3 invitations" above a caption promising it only retried failures,
      // and one press invited the three contractors. Invitations cannot be un-sent.
      onInvited();
    } catch (error) {
      setFailure(classifyApiError(error).message);
    } finally {
      setSending(false);
    }
  }

  const ready = preview ? selected(preview).length : 0;
  const canCheck = (file !== null || pasted.trim().length > 0) && !checking;

  return (
    <section
      aria-labelledby={headingId}
      className="rounded-2xl border border-hairline bg-surface/40 p-6 [@media(max-height:820px)]:p-4 sm:p-7"
    >
      <h2 id={headingId} className="display text-lg font-semibold">
        Add several people
      </h2>
      <p className="mt-2 max-w-prose text-sm leading-relaxed text-muted-foreground">
        Paste a list of work emails, or drop in the spreadsheet you already have. Nothing
        is sent until you have read the preview.
      </p>

      <div className="mt-5 grid gap-5 lg:grid-cols-[1fr_auto]">
        <div className="flex flex-col gap-2">
          <label htmlFor={pastedId} className="text-sm font-medium text-foreground">
            Work emails
          </label>
          <textarea
            id={pastedId}
            value={pasted}
            rows={5}
            disabled={file !== null}
            onChange={(event) => {
              setPasted(event.target.value);
              reset();
            }}
            placeholder={"ada@example.com\nbabbage@example.com, hopper@example.com"}
            aria-describedby={`${pastedId}-hint`}
            className="min-h-32 rounded-xl border border-hairline-strong bg-surface/40 px-3.5 py-3 font-mono text-sm text-foreground placeholder:text-muted-foreground/60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-50"
          />
          <p id={`${pastedId}-hint`} className="text-xs text-muted-foreground">
            One per line, or separated by commas. Pasting straight from your mail client
            works — the names are stripped off.
          </p>
        </div>

        <div className="flex flex-col gap-2 lg:w-64">
          <label htmlFor={roleId} className="text-sm font-medium text-foreground">
            Role for everyone
          </label>
          <select
            id={roleId}
            value={defaultRole}
            onChange={(event) => {
              setDefaultRole(event.target.value as Role);
              reset();
            }}
            className="h-11 rounded-xl border border-hairline-strong bg-surface/40 px-3.5 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
          >
            {grantable.map((role) => (
              <option key={role} value={role}>
                {ROLE_LABELS[role] ?? role}
              </option>
            ))}
          </select>
          <p className="text-xs text-muted-foreground">
            A <code className="font-mono text-[0.7rem]">role</code> column in your file
            wins over this. You can change any row in the preview.
          </p>
        </div>
      </div>

      {/* The drop zone. A visually-hidden file input plus a label pointing at it, so the
          whole area is a real, keyboard-reachable control rather than a div with a click
          handler — and so a drop and a click land in the same place. */}
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          chooseFile(event.dataTransfer.files[0] ?? null);
        }}
        className={cn(
          "mt-5 rounded-xl border border-dashed px-5 py-6 transition-colors",
          dragging ? "border-brand bg-brand/5" : "border-hairline-strong bg-surface/20",
        )}
      >
        {/* **First, so `peer-focus-visible` on the label below can see it.** Tailwind's
            `peer` variant compiles to a following-sibling combinator, so an input placed
            after the label can never style it — which is how the only keyboard-reachable
            control here ended up with no visible focus state at all. */}
        <input
          ref={fileInput}
          id={fileId}
          type="file"
          accept={ACCEPTED.join(",")}
          // Its own name, not the label's. The `<label htmlFor>` below lives inside the
          // `file ? … : …` branch and unmounts the moment a file is chosen, which left a
          // focusable control with no accessible name at all.
          aria-label="Choose a CSV or Excel file of addresses"
          className="peer sr-only"
          onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
        />
        {file ? (
          <div className="flex flex-wrap items-center gap-3">
            <Paperclip aria-hidden="true" className="size-4 text-muted-foreground" />
            <span className="text-sm text-foreground">{file.name}</span>
            <span className="text-xs text-muted-foreground">
              {Math.max(1, Math.round(file.size / 1024))} KB
            </span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                chooseFile(null);
                if (fileInput.current) fileInput.current.value = "";
              }}
              className="ml-auto h-8 gap-1.5 text-xs"
            >
              <X aria-hidden="true" className="size-3.5" />
              Remove {file.name}
            </Button>
          </div>
        ) : (
          // `peer-focus-visible`, not `focus-within`. The input is a SIBLING of this
          // label rather than a child, so `focus-within` could never match it — and
          // because the input is `sr-only`, the browser drew its own focus ring on a
          // clipped 1px box. Keyboard users saw nothing at all move.
          <label
            htmlFor={fileId}
            className="flex cursor-pointer flex-wrap items-center gap-3 rounded-lg text-sm text-muted-foreground peer-focus-visible:outline-2 peer-focus-visible:outline-offset-4 peer-focus-visible:outline-brand"
          >
            <Upload aria-hidden="true" className="size-4" />
            <span>
              Drop a CSV or Excel file here, or{" "}
              <span className="font-medium text-brand underline underline-offset-4">
                choose one
              </span>
              .
            </span>
            <span className="text-xs">
              A column called <code className="font-mono text-[0.7rem]">email</code>, and
              optionally <code className="font-mono text-[0.7rem]">role</code> and{" "}
              <code className="font-mono text-[0.7rem]">role_title</code>.
            </span>
          </label>
        )}
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        <Button
          type="button"
          onClick={check}
          disabled={!canCheck}
          aria-busy={checking}
          className="h-11 rounded-xl px-5"
        >
          {checking ? (
            <>
              <Loader2
                aria-hidden="true"
                className="size-4 animate-spin motion-reduce:animate-none"
              />
              Checking…
            </>
          ) : (
            "Check the list"
          )}
        </Button>
        {preview ? (
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              setPasted("");
              chooseFile(null);
              if (fileInput.current) fileInput.current.value = "";
              setFailure(null);
            }}
            className="h-11 rounded-xl px-5"
          >
            Start over
          </Button>
        ) : null}
      </div>

      {failure ? (
        <div className="mt-4">
          <FormError message={failure} />
        </div>
      ) : null}

      {/* **A parse that found nothing is a result, not a blank table.**
          A spreadsheet exported with only a header row, or a file whose addresses sit in
          a column this does not read, both come back with zero rows — and an empty table
          under the heading "Preview" reads as a broken page rather than as an answer. It
          names the two things that actually cause it. */}
      {preview && preview.total === 0 ? (
        <div className="mt-6 rounded-xl border border-hairline bg-surface/20 px-5 py-6">
          <h3 className="display text-base font-semibold">No addresses found</h3>
          <p className="mt-2 max-w-prose text-sm leading-relaxed text-muted-foreground">
            {file
              ? `${file.name} was read, but no email addresses came out of it. Check that the sheet has a column called `
              : "Nothing in that list looked like an email address. Paste one address per line, or "}
            {file ? (
              <>
                <code className="font-mono text-[0.7rem]">email</code>, and that the
                addresses are not on a second sheet.
              </>
            ) : (
              "drop the spreadsheet instead."
            )}
          </p>
        </div>
      ) : null}

      {preview && preview.total > 0 ? (
        <div className="mt-6">
          <div className="flex flex-wrap items-baseline justify-between gap-3">
            <h3 className="display text-base font-semibold">
              {sent ? "What happened" : "Preview"}
            </h3>
            {/* Announced, because after a send the numbers are the entire result and a
                purely visual summary leaves a screen-reader user with nothing. */}
            <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
              {sent
                ? `${sent.sent} invited${sent.failed ? `, ${sent.failed} could not be` : ""}.`
                : `${preview.total} ${preview.total === 1 ? "address" : "addresses"}, ${ready} to invite.`}
            </p>
          </div>

          <div className="mt-3 overflow-x-auto rounded-xl border border-hairline">
            <table className="w-full min-w-[36rem] border-collapse text-sm">
              <caption className="sr-only">
                {sent
                  ? "Each address and whether it was invited."
                  : "Each address, what will happen to it, and the role it will be given."}
              </caption>
              <thead>
                <tr className="border-b border-hairline text-left">
                  {/* Kept after a send. It is the only control over what a RETRY
                      would send, and removing it was how deliberately-unticked rows
                      became un-untickable. */}
                  <th scope="col" className="w-10 px-3 py-2.5">
                    <span className="sr-only">Include</span>
                  </th>
                  <th scope="col" className="px-3 py-2.5 font-medium">
                    Email
                  </th>
                  <th scope="col" className="px-3 py-2.5 font-medium">
                    Role
                  </th>
                  <th scope="col" className="px-3 py-2.5 font-medium">
                    Outcome
                  </th>
                  <th scope="col" className="px-3 py-2.5 font-medium">
                    Detail
                  </th>
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((row, index) => {
                  const actionable = ACTIONABLE.has(row.outcome);
                  const included = actionable && !excluded.has(index);
                  return (
                    <tr
                      key={`${row.email}-${index}`}
                      className={cn(
                        "border-b border-hairline/60 last:border-0",
                        !actionable ? "opacity-70" : "",
                      )}
                    >
                      <td className="px-3 py-2.5">
                        {
                          <input
                            type="checkbox"
                            checked={included}
                            disabled={!actionable}
                            aria-label={
                              sent ? `Retry ${row.email}` : `Invite ${row.email}`
                            }
                            onChange={(event) =>
                              setExcluded((current) => {
                                const next = new Set(current);
                                if (event.target.checked) next.delete(index);
                                else next.add(index);
                                return next;
                              })
                            }
                            className="size-4 rounded border-hairline-strong disabled:opacity-40"
                          />
                        }
                      </td>
                      <td className="px-3 py-2.5 font-mono text-xs text-foreground">
                        {row.email}
                      </td>
                      <td className="px-3 py-2.5">
                        {actionable ? (
                          <select
                            value={overrides[index] ?? row.role}
                            aria-label={`Role for ${row.email}`}
                            onChange={(event) =>
                              setOverrides((current) => ({
                                ...current,
                                [index]: event.target.value as Role,
                              }))
                            }
                            className="h-9 rounded-lg border border-hairline-strong bg-surface/40 px-2 text-xs text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                          >
                            {grantable.map((role) => (
                              <option key={role} value={role}>
                                {ROLE_LABELS[role] ?? role}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <span className="text-xs text-muted-foreground">
                            {ROLE_LABELS[row.role] ?? row.role}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2.5">
                        <OutcomePill outcome={row.outcome} />
                      </td>
                      <td className="px-3 py-2.5 text-xs text-muted-foreground">
                        {row.detail}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {ready > 0 ? (
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <Button
                type="button"
                onClick={send}
                disabled={sending}
                aria-busy={sending}
                className="h-11 rounded-xl bg-brand px-5 text-brand-foreground hover:bg-brand/90"
              >
                {sending ? (
                  <>
                    <Loader2
                      aria-hidden="true"
                      className="size-4 animate-spin motion-reduce:animate-none"
                    />
                    {sent ? "Retrying" : "Sending"} {ready}…
                  </>
                ) : (
                  // The number comes from the same list the request is built from, so
                  // the label cannot promise a different amount of work than it does.
                  // After a send this can only be failures and rows the reader kept
                  // ticked, so it says "Retry" — the previous label offered to "send"
                  // people it had already invited.
                  sent
                    ? `Retry ${ready} ${ready === 1 ? "row" : "rows"}`
                    : `Send ${ready} ${ready === 1 ? "invitation" : "invitations"}`
                )}
              </Button>
              {sent ? (
                // Retrying is safe and is meant to be pressed: an address that already
                // received mail comes back as "already invited" rather than a second
                // message.
                <p className="text-xs text-muted-foreground">
                  Only the ticked rows above are retried. Anyone already invited is
                  left alone.
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

/**
 * Fold the send's results back into the preview, in place.
 *
 * The API answers only about the rows it was asked to act on, and the reader is looking
 * at the whole list — so a naive replacement would drop every row they had unticked or
 * that was already a member. Positions are carried through the request, so each result
 * lands on the row it came from.
 */
function mergeResults(
  rows: BulkRowResult[],
  requested: { row: BulkRowResult; index: number }[],
  results: BulkRowResult[],
): BulkRowResult[] {
  const merged = [...rows];
  requested.forEach(({ index }, position) => {
    const result = results[position];
    if (result) merged[index] = result;
  });
  return merged;
}
