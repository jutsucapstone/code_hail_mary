import { redirect } from "next/navigation";

import { SIGN_IN_PATH } from "@/lib/surfaces";

/**
 * Moved to `/signin`.
 *
 * The form here was never employee-specific: it took a JUTSU ID and a work email, and
 * the API decided where the session landed. Owners and administrators signing back in
 * needed exactly it, and were instead shown a page headed "Employee" — so they did not
 * try it, and the chooser above it asked whether they were setting up an organisation
 * or had just been invited, which after a month as a customer is neither.
 *
 * A redirect rather than a deletion: this path is in the chooser's history, in browser
 * autocomplete, and quite possibly in an onboarding email somebody already sent.
 *
 * `redirect()` and not `permanentRedirect()` — 307 rather than 308. The difference is
 * whether browsers may cache it indefinitely, and a 308 is a promise this URL will never
 * mean anything else. The pilot funnel is still moving; a wrong 308 is unpickable from
 * every browser that saw it, while an unnecessary 307 costs one hop.
 */
export default function EmployeeSignInMoved(): never {
  redirect(SIGN_IN_PATH);
}
