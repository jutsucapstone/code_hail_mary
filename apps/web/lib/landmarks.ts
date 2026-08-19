/**
 * Shared landmark ids.
 *
 * The skip link lives in the root layout and is therefore rendered on every route, while
 * the `<main>` it targets is rendered by whichever page or group layout is active. Naming
 * the id in one place is what keeps those two halves in step.
 *
 * It previously pointed at `#hero`, which exists only on the landing page — so the skip
 * link was dead on /privacy, /terms, every product surface and the 404. A skip link that
 * resolves to nothing is a WCAG 2.4.1 bypass-blocks failure, and it fails silently: focus
 * simply stays where it was, which looks like the key press did nothing.
 */

/** Target of the "Skip to main content" link. Every `<main>` in the app carries it. */
export const MAIN_CONTENT_ID = "main-content";
