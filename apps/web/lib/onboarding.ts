/**
 * The closed sets the organisation form offers.
 *
 * Both are optional on the form and both are constrained in the database (migration
 * 0007), so these lists and the `CHECK` constraints have to agree. Keeping the values
 * here as the single client-side source means a mismatch is a TypeScript error at the
 * call site rather than a 422 the person filling the form in has to interpret.
 */

/** Mirrors `ck_orgs_industry`. Values are stored; labels are only ever displayed. */
export const INDUSTRIES = [
  { value: "consulting", label: "Consulting" },
  { value: "technology", label: "Technology" },
  { value: "finance", label: "Finance" },
  { value: "healthcare", label: "Healthcare" },
  { value: "manufacturing", label: "Manufacturing" },
  { value: "government", label: "Government" },
  { value: "other", label: "Other" },
] as const;

export type Industry = (typeof INDUSTRIES)[number]["value"];

/**
 * ISO 3166-1 alpha-2 codes, stored rather than country names.
 *
 * A code survives a country being renamed and is what every downstream consumer wants —
 * timezone tables, tax and residency rules, and the compliance configuration this is
 * being collected for. Storing "Netherlands" would mean re-deriving the code every time
 * and guessing at "Holland".
 *
 * Only the codes are listed. The display names come from `Intl.DisplayNames`, which is
 * built into the browser, so this ships roughly a kilobyte instead of a localised name
 * table — and each visitor sees the names in their own language for free. That matters
 * here specifically: CLAUDE.md records a fight over ~900KB of deploy weight, and a
 * bundled country list is the same mistake in miniature.
 */
const COUNTRY_CODES = [
  "AE","AR","AT","AU","BE","BG","BH","BR","CA","CH","CL","CN","CO","CY","CZ","DE","DK",
  "EE","EG","ES","FI","FR","GB","GR","HK","HR","HU","ID","IE","IL","IN","IS","IT","JP",
  "KE","KR","KW","LT","LU","LV","MA","MT","MX","MY","NG","NL","NO","NZ","OM","PE","PH",
  "PK","PL","PT","QA","RO","SA","SE","SG","SI","SK","TH","TR","TW","UA","US","VN","ZA",
] as const;

export interface CountryOption {
  readonly value: string;
  readonly label: string;
}

/**
 * Country options, sorted by the name the visitor will actually read.
 *
 * Sorted with a locale-aware comparator, because sorting localised names by code point
 * puts accented names in the wrong place in exactly the languages that have them.
 *
 * Falls back to the bare code if `Intl.DisplayNames` is unavailable — the field is
 * optional, and a list of codes is worse than a list of names but far better than a
 * control that throws during render.
 *
 * **Call this on the client only.** The output depends on the ICU data and default
 * locale of whoever runs it: a browser in `en-US` yields "Argentina" first, while Node
 * built against small-icu yields the bare code and therefore a completely different sort
 * order. Rendering it during SSR produced a hydration mismatch on every load — found by
 * the dev overlay on the real page, not by review.
 */
export function countryOptions(locale?: string): CountryOption[] {
  let names: Intl.DisplayNames | null = null;
  try {
    names = new Intl.DisplayNames(locale ? [locale] : undefined, { type: "region" });
  } catch {
    names = null;
  }

  const options = COUNTRY_CODES.map((value) => ({
    value,
    label: names?.of(value) ?? value,
  }));

  return options.sort((a, b) => a.label.localeCompare(b.label, locale));
}
