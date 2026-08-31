"use client"; // Error boundaries must be Client Components.

/**
 * The last boundary: the root layout itself failed.
 *
 * This file *replaces* the root layout when it renders, which has three consequences that
 * are easy to get wrong and impossible to notice in development:
 *
 * 1. **It must render its own `<html>` and `<body>`.** There is no document above it.
 * 2. **`app/globals.css` is not loaded, and neither are the `next/font` variables.** The
 *    stylesheet and the `--font-sans` custom property both arrive via the root layout that
 *    is currently broken, so every Tailwind class here would be inert and every token
 *    undefined. The styles are inline for that reason, not out of haste, and the type
 *    stack is the system one.
 * 3. **`next-themes` is not mounted**, so there is no `.dark` class to key off. Theme comes
 *    from `prefers-color-scheme` alone — a reader who chose light while their OS is dark
 *    gets the dark rendering here, which is the documented behaviour and better than the
 *    unstyled white page that is the alternative.
 *
 * `metadata` is not supported in a Client Component, so the tab name comes from React's
 * `<title>`.
 */
export default function GlobalError({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return (
    <html lang="en">
      <body>
        <title>Something went wrong · JUTSU</title>
        <style>{`
          :root {
            --ge-bg: #f7f7f9;
            --ge-fg: #0f1113;
            --ge-muted: #5b625e;
            --ge-line: rgba(15, 17, 19, 0.14);
            --ge-brand: #499f02;
            --ge-brand-fg: #ffffff;
          }
          @media (prefers-color-scheme: dark) {
            :root {
              --ge-bg: #0a0b0f;
              --ge-fg: #edf1ed;
              --ge-muted: #99a39d;
              --ge-line: rgba(237, 241, 237, 0.16);
              --ge-brand: #83d005;
              --ge-brand-fg: #0a0b0f;
            }
          }
          * { box-sizing: border-box; }
          body {
            margin: 0;
            min-height: 100dvh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 1.5rem;
            background: var(--ge-bg);
            color: var(--ge-fg);
            font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
            line-height: 1.6;
            -webkit-font-smoothing: antialiased;
          }
          .ge-card { max-width: 32rem; width: 100%; }
          .ge-eyebrow {
            display: flex; align-items: center; gap: 0.6em;
            margin: 0 0 1rem;
            font-family: ui-monospace, "SF Mono", Menlo, monospace;
            font-size: 0.6875rem; font-weight: 500;
            letter-spacing: 0.14em; text-transform: uppercase;
            color: var(--ge-brand);
          }
          .ge-eyebrow::before {
            content: ""; width: 4px; height: 4px; border-radius: 50%;
            background: currentColor; flex: none;
          }
          .ge-title {
            margin: 0; font-size: 1.75rem; font-weight: 600;
            letter-spacing: -0.03em; line-height: 1.15; text-wrap: balance;
          }
          .ge-body { margin: 0.85rem 0 0; color: var(--ge-muted); font-size: 0.9375rem; }
          .ge-ref {
            margin: 0.85rem 0 0;
            font-family: ui-monospace, "SF Mono", Menlo, monospace;
            font-size: 0.6875rem; letter-spacing: 0.14em; text-transform: uppercase;
            color: var(--ge-muted);
          }
          .ge-actions { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-top: 1.75rem; }
          .ge-btn {
            font: inherit; font-size: 0.875rem; font-weight: 500;
            padding: 0.55rem 1rem; border-radius: 0.5rem;
            cursor: pointer; text-decoration: none;
            border: 1px solid var(--ge-line);
            background: transparent; color: var(--ge-fg);
          }
          .ge-btn--primary {
            background: var(--ge-brand); color: var(--ge-brand-fg);
            border-color: var(--ge-brand);
          }
          .ge-btn:focus-visible { outline: 2px solid var(--ge-brand); outline-offset: 2px; }
        `}</style>

        <div className="ge-card" role="alert">
          <p className="ge-eyebrow">Error</p>
          <h1 className="ge-title">JUTSU could not finish loading.</h1>
          <p className="ge-body">
            Something failed before the page could be built. Your data is unaffected and you
            are still signed in — this is not something you did.
          </p>
          {error.digest ? <p className="ge-ref">Reference {error.digest}</p> : null}
          <div className="ge-actions">
            <button type="button" className="ge-btn ge-btn--primary" onClick={() => retry()}>
              Try again
            </button>
            {/* A plain anchor, deliberately, and the one place in the app where that is
                correct. `next/link` performs a *client-side* navigation using the router
                that lives in the root layout — the layout which, by definition, has just
                failed to render. A full document load is the recovery; a soft navigation
                would hand the reader back to the same broken tree. */}
            {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
            <a className="ge-btn" href="/">
              Go to the home page
            </a>
          </div>
        </div>
      </body>
    </html>
  );
}
