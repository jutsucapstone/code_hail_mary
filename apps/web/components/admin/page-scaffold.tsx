"use client";

import { cn } from "@/lib/utils";

/**
 * The admin console's shared page grammar.
 *
 * Extracted the day the console grew from three sections to ten: the eyebrow-heading
 * pattern, the stat strip and the scrolling table existed as copies in two pages, and
 * seven more copies would have guaranteed drift — one page's sticky header opaque,
 * another's translucent, for no reason a reader could infer.
 */

export function PageHeader({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <header>
      <p className="eyebrow flex items-center gap-2.5 text-brand">
        <span aria-hidden="true" className="h-1 w-1 rounded-full bg-brand" />
        {eyebrow}
      </p>
      <h1 className="display mt-4 text-3xl font-semibold [@media(max-height:820px)]:mt-2 [@media(max-height:820px)]:text-2xl sm:text-4xl">
        {title}
      </h1>
      {children ? (
        <p className="mt-3 max-w-prose text-pretty text-sm leading-relaxed text-muted-foreground">
          {children}
        </p>
      ) : null}
    </header>
  );
}

/** One figure in a stat strip. The value is always a real measurement (§4.11). */
export interface Stat {
  id: string;
  label: string;
  value: number | string;
  icon?: React.ComponentType<{ className?: string }>;
}

export function StatStrip({ stats, columns = 3 }: { stats: Stat[]; columns?: 2 | 3 | 4 }) {
  const cols =
    columns === 4 ? "sm:grid-cols-2 lg:grid-cols-4" : columns === 2 ? "sm:grid-cols-2" : "sm:grid-cols-3";
  return (
    <dl className={cn("grid gap-px overflow-clip rounded-2xl border border-hairline bg-hairline", cols)}>
      {stats.map((stat) => (
        <div
          key={stat.id}
          className="flex flex-col gap-3 bg-background p-6 [@media(max-height:820px)]:gap-2 [@media(max-height:820px)]:p-4 lg:p-7"
        >
          {stat.icon ? (
            <span
              aria-hidden="true"
              className="flex size-9 items-center justify-center rounded-lg border border-hairline-strong bg-surface text-brand"
            >
              <stat.icon className="size-4" />
            </span>
          ) : null}
          {/* dd before dt so the figure reads first visually; the pairing is still
              correct for assistive technology, which follows the markup. */}
          <dd className="display text-3xl font-semibold tabular-nums">{stat.value}</dd>
          <dt className="text-sm text-muted-foreground">{stat.label}</dt>
        </div>
      ))}
    </dl>
  );
}

/**
 * The scrolling table container the employees page proved out.
 *
 * The TABLE scrolls, not the page: bounded here so the chrome stays put however many
 * rows there are. `relative` is load-bearing — a static scroll box is not a containing
 * block, so the table's min-width would escape and stretch the page sideways.
 */
export function TableShell({
  caption,
  headings,
  minWidth = "min-w-[44rem]",
  children,
}: {
  caption: string;
  headings: string[];
  minWidth?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="relative min-h-0 flex-1 overflow-auto rounded-2xl border border-hairline-strong">
      <table className={cn("w-full border-collapse text-sm", minWidth)}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          {/* Sticky on each cell rather than on <thead>: a sticky thead is still not
              honoured consistently, and the headings must stay readable once rows start
              scrolling under them. Opaque background so rows do not show through. */}
          <tr className="text-left">
            {headings.map((heading) => (
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
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/**
 * A word-first status pill. The word is the signal; the tint only reinforces it —
 * colour-only status is unreadable to a screen reader and to anyone who cannot
 * distinguish the hue.
 */
export function Pill({
  tone,
  children,
}: {
  tone: "good" | "attention" | "bad" | "neutral";
  children: React.ReactNode;
}) {
  const tint =
    tone === "good"
      ? "bg-brand/12 text-brand"
      : tone === "attention"
        ? "bg-graph/12 text-graph"
        : tone === "bad"
          ? "bg-destructive/12 text-destructive"
          : "bg-muted text-muted-foreground";
  return (
    <span
      className={`inline-flex rounded-full px-2 py-0.5 font-mono text-[0.625rem] uppercase tracking-[0.16em] ${tint}`}
    >
      {children}
    </span>
  );
}

/** "Load more" for keyset pages. Rendered only while there is genuinely more. */
export function LoadMore({
  onClick,
  pending,
}: {
  onClick: () => void;
  pending: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={pending}
      className="self-start rounded-lg border border-hairline-strong px-3.5 py-2 text-sm font-medium transition-colors hover:border-brand/40 hover:bg-brand/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:opacity-60"
    >
      {pending ? "Loading…" : "Load more"}
    </button>
  );
}

/** A datetime, in the reader's locale, that machines can still parse. */
export function When({ iso }: { iso: string | null | undefined }) {
  if (!iso) return <span className="text-muted-foreground">—</span>;
  const date = new Date(iso);
  return (
    <time dateTime={iso} className="tabular-nums">
      {date.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })}
    </time>
  );
}
