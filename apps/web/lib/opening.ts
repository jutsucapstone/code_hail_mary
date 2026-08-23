/**
 * The cold open shows once per visitor, then never again.
 *
 * The mechanism is the one `next-themes` uses to avoid a theme flash, and for the same
 * reason: the decision has to be made *before first paint*, and it depends on state only
 * the browser holds.
 *
 * Reading a cookie in the page component would be simpler, but it would make the landing
 * page dynamic — Next currently prerenders it as static HTML, and giving that up for one
 * boolean is a poor trade on the page most likely to be someone's first impression.
 * Deciding in React instead would render the section and then remove it, so a returning
 * visitor would watch 220vh of content vanish and the page jump.
 *
 * So a tiny synchronous script stamps an attribute on `<html>` during parse, and CSS
 * hides the section. No flash, no layout shift, no dynamic rendering.
 */

/** Where the flag lives. Namespaced so it cannot collide with anything else on the origin. */
export const OPENING_SEEN_KEY = "jutsu.opening-seen";

/** Stamped on `<html>` when the flag is set. `globals.css` keys the hiding rule off it. */
export const OPENING_SEEN_ATTR = "data-opening-seen";

/** The id of the section itself, shared so the script, the CSS and the markup agree. */
export const OPENING_SECTION_ID = "manifesto";

/**
 * Runs before the body is parsed, so the section is hidden in the very first paint.
 *
 * Wrapped in try/catch because `localStorage` throws outright in some privacy modes, and
 * an exception here would abort the script and leave the page unstyled below it. Failing
 * this check simply shows the cold open again, which is the harmless direction to fail.
 */
export const OPENING_SEEN_SCRIPT = `try{if(localStorage.getItem(${JSON.stringify(
  OPENING_SEEN_KEY,
)})==="1"){document.documentElement.setAttribute(${JSON.stringify(
  OPENING_SEEN_ATTR,
)},"1")}}catch(e){}`;
