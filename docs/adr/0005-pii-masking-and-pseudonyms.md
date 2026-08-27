# ADR 0005 — PII pseudonyms are scoped to a document, and only checkable types are detected

- **Status:** accepted
- **Date:** 2026-08-27
- **Slice:** S4

## Context

§9.1 specifies masking as `mask(text, detectors) -> MaskResult`, with a `token` that is
"stable within a document" and a `vault_key` per span. It does not say what the token is
derived from, which types ship with a detector, or what an offset landing *inside* a
token translates to. All three have to be answered before anything can be chunked, and
each of them is load-bearing in a different direction — one for privacy, one for honesty
about what "masked" means, one for whether citations highlight the right words.

The masked text is not a private artefact. It is what reaches the LLM, what gets embedded
into `chunks.text`, and what any future prompt-log or eval export contains. Whatever
structure the tokens carry is structure that travels with it.

## Decision 1 — the token is derived from the document as well as the value

`[EMAIL_A7]` comes from a digest over `(namespace, type, canonical value)`, where
`namespace` is the document (the ingestion pipeline passes the document id; a bare
`mask(text)` falls back to the hash of the text, which keeps the function
self-contained and deterministic for tests and evals).

### Options

1. **Sequential per document** — `[EMAIL_1]`, `[EMAIL_2]` by first appearance. Rejected:
   equally private, but the token then depends on document *order* rather than on
   content, so inserting a sentence renumbers every token after it and the whole masked
   text — and therefore every embedding — churns on an edit that changed one line.
2. **A pure function of the value** — the same address is `[EMAIL_A7]` in every document
   in the corpus. Rejected, and this is the important one. It would make cross-document
   entity resolution easy, and it would also make the masked corpus a correlation table:
   anyone who can read masked text — an eval export, a prompt log, a support dump, a
   retrieval response — could count how many documents an individual appears in and which
   ones, without ever holding a vault key or passing an ACL check. Masking that yields a
   stable global pseudonym is pseudonymisation in name only.
3. **Document-scoped digest.** Accepted.

### Consequences

- Co-reference within a document works, which is what extraction needs: three mentions of
  one address are one token and three spans.
- Co-reference *across* documents does not come free from the token, and must go through
  the vault under the ACL check — which is the correct place for it, and which S7 and the
  extraction slices will have to route through explicitly rather than by accident.
- Two characters of Crockford base32 gives 1024 pseudonyms per type per document, so
  collisions are ordinary rather than negligible: thirty entities collide about a third of
  the time. `_allocate_token` therefore re-derives on collision and widens the suffix
  after eight attempts. `test_tokens_stay_distinct_across_many_entities` runs two hundred
  entities through one document, where a hope-based implementation fails outright.
- `vault_key` is namespaced the same way, because `pii_vault.vault_key` is a primary key
  on its own while the same address genuinely appears in thousands of documents.

## Decision 2 — ship detectors that can be checked; ship no detector at all for PERSON and ADDRESS

`PiiType` carries six types. S4 ships five detectors covering four of them: EMAIL, PHONE,
GOV_ID (US SSN), and FINANCIAL (payment card by Luhn, IBAN by mod-97). PERSON and ADDRESS
have **no** detector.

Named-entity recognition needs a model. That is a stack decision — a dependency, a
serving cost, a latency budget and an evaluation of its own — and §5 fixes the stack, so
it is not something a slice on offset arithmetic gets to decide on the way past.

The alternative considered and rejected was a capitalised-bigram heuristic for names. It
would find perhaps half of them, and the half it missed would be invisible: every
downstream measurement — the eval harness, a DPIA, the sentence "the corpus is masked" —
would then be computed against a corpus believed to be masked and not being so. §4.11
forbids exactly this shape of thing behind a UI; it is worse in a privacy control than in
a dashboard.

`mask` handles PERSON and ADDRESS the moment a detector for them is passed in — the
protocol, the canonicalisers and the token machinery are all type-agnostic and tested
through stub detectors on those two types. What is missing is a detector, not support.

**Until one lands, "masked" means: no addresses, phone numbers, SSNs, cards or IBANs. It
does not mean no names.** That sentence belongs in the DPIA, not only in this file.

## Decision 3 — pattern detectors prefer precision, and the misses are tested

Every false positive is a real word removed from a chunk that will be embedded, so a
loose detector costs retrieval quality permanently in exchange for privacy it did not
actually provide. The detectors therefore:

- validate with a checksum wherever the data type has one (Luhn, mod-97) rather than
  trusting the shape;
- encode the ranges a US SSN cannot occupy in the pattern itself;
- refuse a bare run of digits with no `+` and no separators, which is an identifier far
  more often than a phone number;
- refuse to start immediately after a letter or digit, so `SN123-456-7890` stays a part
  number.

The last of those is a genuine miss for a number written with nothing before it, and it is
recorded as a passing test (`test_a_number_glued_to_a_word_is_left_alone`) rather than a
comment, so that widening the pattern later is a deliberate act with a failing test
attached.

## Decision 4 — an offset inside a token resolves to the start of the run it replaced

A token stands in for a run of a different length, so there is no character-by-character
correspondence and `to_original` has to answer *something* for an interior offset. It
answers `orig_start`. That keeps the function monotonic, and it never returns a position
part-way through somebody's name.

For ranges — which is what the chunker actually needs — `original_range` applies start
semantics to one end and end semantics to the other, rounding outward, so a slice that
clips a token still yields an original range covering the whole entity and can never
invert. §9.2 forbids splitting inside a span in the first place; this is what stops a
broken promise there becoming a citation that highlights half an email address.

The two ends use `bisect_right` and `bisect_left` respectively, and the difference is not
stylistic: an offset sitting exactly on a token's `masked_start` is the first character
*of* that token when it is a start, and the position just *before* it when it is an end.
Using `bisect_right` for both makes a chunk ending immediately before an address record
itself as covering the address. That bug was written, and the test
`test_original_range_of_a_clean_slice_is_two_translations` is what found it.
