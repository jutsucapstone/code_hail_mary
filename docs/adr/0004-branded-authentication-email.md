# ADR 0004 — Branded authentication email, and where the organisation identifier may appear

- **Status:** accepted
- **Date:** 2026-08-26
- **Slice:** S1

## Context

Every JUTSU authentication email was a plain-text sentence with the code appended by the
SMTP transport. Three flows shared two of them:

| Flow | Message |
|---|---|
| Organisation registration (staging) | `"Your JUTSU sign-in code is below…"` |
| Returning sign-in | the same string |
| Employee invitation | `"Your organisation has invited you to JUTSU…"` |

Two problems, one cosmetic and one not.

The cosmetic one: a one-time code arriving as unstyled text from a `noreply@` address is
what phishing looks like, and it is the first thing a pilot customer sees of the product.

The one that is not cosmetic: the brief asked for the **organisation token** to be
delivered during company onboarding, and for it never to appear in a returning user's
sign-in mail. The existing code had no notion of which message may carry what, because
there was only one message. Adding a branded template per flow without also deciding that
question would have produced six templates and a convention nobody could enforce.

A third problem surfaced while reading the flow. The organisation does not exist when the
onboarding code is sent. `stage_registration` writes one row in `auth.pending_registrations`
and mails a challenge; the organisation, its owner and the owner's JUTSU ID all come into
existence in `complete_registration`, on the verify side. That ordering is a security
control — it is what stops a domain being claimed by anyone who can type it — and it means
there is no identifier in existence to put in the first message.

## Options

1. **Mint the organisation at staging so one message can carry token and code.**
   Rejected outright. It restores the exact hole the staging/verify split was introduced
   to close: `uq_orgs_domain_active` would let an anonymous caller claim `microsoft.com`
   without reading any inbox.

2. **One template with optional fields, populated per flow.** Rejected. An optional field
   is a field somebody eventually populates from the wrong call site, and the wrong call
   site here is the sign-in mail every employee receives forever. Nothing in the type
   system would object.

3. **Two messages for onboarding, and a narrow function per message.** Accepted.

## Decision

`jutsu_api.emails` holds one component system — `theme`, `components`, `layout` — and
six message builders. Onboarding is two messages, in the order the flow already runs in:

| Message | When | Carries |
|---|---|---|
| `organisation_verification` | staging, before any tenant exists | OTP, company name and domain as typed |
| `organisation_welcome` | completion, the moment the tenant exists | organisation id, owner's JUTSU ID, role |
| `employee_invitation` | an admin invites somebody | invitation token, organisation name |
| `employee_welcome` | that person accepts | their own JUTSU ID and role |
| `sign_in_code` | every return sign-in | OTP only |
| `no_account` | an address with no membership | nothing |

**The containment rule is enforced by the signatures, not by review.**
`organisation_welcome` is the only builder with an `org_id` parameter. The others cannot
carry one because there is nowhere to put it, and `test_email_templates.py` asserts that
property against `inspect.signature` — so widening `sign_in_code` fails a test while the
template is still innocent, rather than after a tenant identifier has been mailed to every
employee for a month.

A person's own JUTSU ID is deliberately **not** treated as an organisation token.
`/signin` asks a returning employee for it by name, and acceptance currently shows it on
one screen that is gone when the tab closes.

Two supporting decisions:

- **URLs are built plain and escaped once, by whoever puts them in markup.** The first
  cut pre-escaped the `&` in `?token=…&flow=register`, and `action_button` escaped the
  href again — `&amp;amp;` on the wire, which a browser reads as a parameter named
  `amp;flow`. `/pilot/verify` uses `flow=register` to choose between `completeRegistration`
  and `verify`, so the emailed button submitted every registration code to the sign-in
  endpoint: refused on `expected_purpose`, and one of five attempts gone. Caught by
  parsing the rendered hrefs rather than searching them — `"flow=register" in html` is
  satisfied by the broken string, which is why that assertion had passed.
- **Secrets stay out of the templates.** A template emits `[[code]]` and `[[token]]`;
  `fill_secrets` substitutes at delivery, inside the transport, which was already the only
  place a one-time value was allowed to become text. A rendered template therefore
  contains no live credential and is safe to snapshot, diff and assert against.
- **The template and "does this carry a credential" are one decision.** `_challenge_message`
  returns both, because keying the secrets off `known_account` alone meant a registration
  challenge for an address with no membership would have selected a template full of
  placeholders and delivered it with nothing to fill them. Only `stage_registration` issues
  those and it always passes `True`, so it was unreachable — returning the pair makes it
  unrepresentable instead.
- **The welcome messages are best-effort.** They are sent from the routers, inside the
  transaction that created the tenant or the membership. Raising would roll that back over
  a refused SMTP connection — and the registrant could not retry, because the challenge and
  the staged payload are both consumed by then. The code-bearing messages stay fatal:
  somebody is waiting on those.

## Consequences

- Company onboarding now sends **two** emails rather than one. The organisation identifier
  arrives after verification because it does not exist before it. Anything that wants it in
  the first message has to move tenant creation ahead of mailbox proof, which is the trade
  this ADR refuses.
- Employee acceptance now sends an email where it previously sent none. It carries no
  credential — the invitation already proved the mailbox — and no second OTP is issued.
- The API needs to know the console's absolute origin for the links. `JUTSU_APP_URL`,
  defaulted per environment (`https://jutsu.co.in` in prod, `http://localhost:3210`
  otherwise) rather than required, because a missing value cannot be caught at boot the way
  a missing SMTP credential can — the service would start, authenticate people, and only
  produce a broken link in mail nobody on the team reads.
- The mark is attached as a `multipart/related` part, not fetched over https. Outlook
  blocks remote images by default; an authentication email whose branding appears only
  after the reader clicks "display images" looks exactly like what it is trying not to be.
  It costs ~16KB of base64 per message, against Gmail's ~102KB clipping threshold; the
  largest message is under 32KB and a test holds that line.
- `ROLE_LABELS` moved into `jutsu_core.rbac`. `"hr_admin".title()` is `"Hr Admin"`, and the
  admin console will want the same strings.
