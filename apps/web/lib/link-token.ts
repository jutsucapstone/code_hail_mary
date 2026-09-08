"use client";

import { useEffect, useState } from "react";

/**
 * Reading the one-time values that arrive with a person rather than with a request.
 *
 * **A token belongs in the URL fragment, never the query string.** A browser does not
 * transmit a fragment, so `#token=…` reaches this code and reaches no server — while
 * `?token=…` is sent with every request for that URL and written verbatim into the Cloud
 * Run request log as `httpRequest.requestUrl`, where it sits for thirty days. An
 * invitation token is a single-factor account-creation credential, so that was secret
 * exposure rather than untidiness, and it was confirmed in production Cloud Logging
 * rather than reasoned about.
 *
 * **An address that only the next screen needs belongs in `sessionStorage`.** `?to=<email>`
 * put registrants' and employees' work addresses into that same log, for the sole purpose
 * of letting one screen say "we sent a code to …".
 *
 * Both hooks read with a lazy `useState` initialiser rather than assigning state inside an
 * effect. That is not a style choice: `setState` called synchronously in an effect
 * triggers a cascading render, which the project's lint rules refuse — correctly, since
 * the value here is known at first render and never changes.
 */

/** Where the sign-in and registration screens leave the address they just sent a code to. */
export const VERIFY_ADDRESS_KEY = "jutsu:verify-address";

function readParam(param: string): string {
  try {
    const url = new URL(window.location.href);
    const fromFragment = new URLSearchParams(url.hash.replace(/^#/, "")).get(param);
    // The query string is still read as a fallback. Links already delivered to real
    // inboxes carry `?token=`, and refusing them would break every invitation sent
    // before this shipped — a token in a log is a thing to stop repeating, not a reason
    // to lock people out of mail they already hold.
    return fromFragment ?? url.searchParams.get(param) ?? "";
  } catch {
    return "";
  }
}

/**
 * The one-time token from an emailed link, and gone from the address bar once read.
 *
 * `history.replaceState` rewrites the entry in place, which keeps the token out of
 * anything the person copies, bookmarks or screenshots, and leaves no extra history
 * entry — so Back still goes where the reader expects.
 */
export function useLinkToken(param = "token"): string {
  // `typeof window` because a client component is still rendered once on the server, and
  // there is no fragment there — the browser is the only participant that has ever seen
  // it. The initialiser runs once, so the value survives the scrub below.
  const [token] = useState(() => (typeof window === "undefined" ? "" : readParam(param)));

  useEffect(() => {
    if (!token) return;
    try {
      const url = new URL(window.location.href);
      url.hash = "";
      url.searchParams.delete(param);
      window.history.replaceState(null, "", `${url.pathname}${url.search}`);
    } catch {
      // A URL this cannot parse is one there was nothing to scrub from.
    }
  }, [token, param]);

  return token;
}

/**
 * Read a same-tab hand-off value, and consume it.
 *
 * Consumed rather than left behind so a stale address cannot be shown on a later,
 * unrelated visit to the same screen — which would tell somebody a code went to an
 * address this attempt never touched.
 *
 * Every access is wrapped: `sessionStorage` throws outright in some privacy modes, and a
 * missing value is an ordinary outcome here — the screen simply omits the sentence.
 */
export function useHandoff(key: string): string | null {
  const [value] = useState(() => {
    if (typeof window === "undefined") return null;
    try {
      return window.sessionStorage.getItem(key);
    } catch {
      return null;
    }
  });

  useEffect(() => {
    if (!value) return;
    try {
      window.sessionStorage.removeItem(key);
    } catch {
      // Nothing to remove if it could not be read either.
    }
  }, [value, key]);

  return value;
}

/** Write a same-tab hand-off value. Silent when storage is unavailable. */
export function setHandoff(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // The next screen omits the sentence that would have used it.
  }
}
