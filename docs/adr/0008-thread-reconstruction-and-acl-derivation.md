# ADR 0008 — Threads are components, and ACLs come from the headers

- **Status:** accepted
- **Date:** 2026-08-27
- **Slice:** S3

## Context

§19 requires the Enron sample to take **complete threads, never random messages**, and
warns that random sampling shreds the reply graph and leaves entity resolution with
nothing to resolve — a failure that surfaces in week five, in a component that looks
unrelated. Two questions have to be answered before that is implementable, and neither is
in the spec.

**What is a thread?** Mail gives three signals — `Message-ID`, `In-Reply-To`,
`References` — and all three lie. `References` is truncated from the middle by clients
once it grows. `In-Reply-To` names a message that may not be in the corpus. Ids repeat,
cycle, and go missing entirely: a substantial fraction of real messages carry no
`Message-ID` at all.

**Who may read a document?** §12 filters retrieval on `document_acl`, and the seven
adversarial tests of §17 need real grant variety to mean anything. The Enron corpus ships
no ACLs.

## Decision 1 — a thread is a connected component over message ids

Union-find over every id a message mentions: its own, its `In-Reply-To`, and every entry
in its `References`. Not a parent-pointer walk.

### Why

- **Truncation stops mattering.** Two messages that each retain a different surviving
  fragment of a long `References` chain still join, because they union through whatever
  ids they do carry.
- **Cycles cannot hang it.** `A` references `B` while `B` references `A` occurs in real
  corpora through forwarded loops and broken clients. A parent walk spins; union-find is
  indifferent.
- **A missing parent still joins its children.** Referenced-but-absent ids are first-class
  members of the component, so two replies to a message outside the sample land in one
  thread rather than two. This is the case that matters most for a *sample*, where by
  construction most parents are outside it.
- **A message with no `Message-ID` is still a document.** It is represented by its corpus
  key, namespaced `jutsu-keyed:` so a path-shaped id elsewhere cannot collide with it.

### The thread id is the lexicographically smallest id in the component

A **canonical label, not "the first message"**. Ordering by date was rejected: `Date` is
the least reliable header in the file — missing, unparseable, or zone-less in a
meaningful fraction of messages — and thread *identity* must not depend on it. The
smallest id is a pure function of the component's membership, which is what makes
sampling reproducible.

### Consequence

Two conversations that share a mangled id merge, and there is no way to tell that from
inside the data. This is the accepted failure direction: a merged thread is over-inclusive
(the sample carries more context than needed) while a split thread is the thing §19
forbids.

## Decision 2 — ACLs are derived from the message's own participants

Sender plus every `To`, `Cc` and `Bcc` recipient, as `user` principals, `read` only.

### Why

This is not an invented policy. It is **the access the mail system itself granted**,
recovered from the headers — the people who were on the message are exactly the people
who could read it. Any alternative was worse:

- *A single `org` grant for everything.* Rejected: it makes every §17 adversarial test
  vacuous, because there is no document any member cannot see.
- *Synthesised random grants.* Rejected outright. That is fabricated authorisation data,
  and every ACL measurement taken against it would be measuring the generator.
- *No grants at all.* Rejected: a document with no grants is invisible, so the corpus
  would ingest and retrieve nothing.

`Bcc` is included. The corpus preserves it in the sender's own copy, and a blind recipient
did receive the message; excluding them produces a grant set that is wrong in the
direction that hides evidence from somebody who legitimately saw it.

### SUPERSEDED (2026-08-28, S6.5): see ADR 0010

The concern below was correct and has been addressed. Principals are now namespaced
`{source_system}:{subject}`, so this connector emits `local:someone@example.com` rather
than a bare address, and `source_identities` maps a signed-in user to the subjects they
own. The address is still the subject for this corpus — a public mail archive has no
identity provider — but it is now labelled as a `local:` subject instead of looking like
a portable one.

**Email is not an authorization identity. A source identity is a provider-native immutable
subject.** ADR 0010 has the reasoning.

### The part that will not survive contact with a real tenant

**Principal ids are email addresses.** `document_acl.principal_id` is matched against
`users.external_id`, which for a real tenant holds an **IdP subject** — so an address in
that column matches nothing the day a customer connects. This mapping is corpus-specific
and a provider connector will emit subjects instead.

It is recorded here rather than left to be discovered because the failure is silent: ACL
filtering with principal ids that match nothing returns an empty result set, which looks
exactly like a correctly-filtered query returning nothing.

### Also consequential

- A message whose participants cannot be parsed gets **no grants**, and is therefore
  invisible rather than public. Failing closed is the right direction and it is tested.
- Grants are sorted before hashing, so `acl_hash_of` detects permission drift and not
  header order.

## Decision 3 — three departures from §19's sketch

§19 sketches `sample_enron(...) -> Iterator[RawDocument]` that emits a manifest as a side
effect. Implemented as `sample_enron(...) -> SampleResult`, plus a separate
`load_documents`.

- **The manifest is returned, not written.** A function that returns its result is
  testable; one that writes a file needs a temporary directory before anything can be
  asserted about it. The caller writes it.
- **Selection reads headers only.** Materialising half a million documents to choose fifty
  thousand of them is the difference between a minute and an afternoon. `load_documents`
  streams the bodies afterwards.
- **A thread that does not fit stops the sample.** Skipping it and continuing packs the
  budget more tightly and **biases the sample toward short threads** — precisely the
  property the corpus is being sampled for. Stopping undershoots the target *visibly*, and
  the manifest records `target_messages` alongside `sampled_messages` so the shortfall is
  a number rather than a surprise. Truncating a thread to fit is not an option and never
  will be.

### Byte-identical manifests

§19 requires the same seed to reproduce the manifest exactly. That constrains everything:
every list is sorted, custodian ties break by name, the thread order is a seeded shuffle
of a *sorted* list, and **the manifest carries no timestamp**. A `generated_at` field is
the one addition guaranteed to break the requirement on every run.

`random.Random(seed)` is used deliberately and carries a `# noqa: S311`. Bandit is right
that it is not cryptographically secure, which is exactly why it fits: a CSPRNG cannot be
seeded to repeat, and repeatability is the requirement.

## Decision 4 — fixtures are built in code, not committed as files

The maildir fixtures are constructed from Python rather than checked in as `.eml`.
`.gitattributes` sets `* text=auto eol=lf`, so git would rewrite CRLF line endings in a
committed mail file — and **mail is line-ending sensitive**, since a MIME boundary is
defined in terms of CRLF. Committed fixtures would parse differently from the files they
imitate, and differently again depending on whose checkout they came from.

These fixtures are **parser fixtures and nothing else**. They are not corpus data, they
are never seeded into Postgres, and no measurement is taken against them. Validation
against the real Enron corpus remains outstanding and is a separately approved step.
