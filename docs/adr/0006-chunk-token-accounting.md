# ADR 0006 — The token counter is injected, and S5's default is an estimate

- **Status:** accepted
- **Date:** 2026-08-27
- **Slice:** S5

## Context

§9.2 sizes a chunk in tokens: `target_tokens: int = 768`, `min_tokens: int = 128`, and a
`token_count` stored on every chunk. Every one of those numbers means *tokens as the
embedding model counts them* — §9.3 fixes that model as Gemini `text-embedding-004`.

`packages/core` cannot count them. Gemini's tokeniser is served over the network
(`count_tokens`), which cannot live inside a pure function that the chunker calls a
handful of times per boundary, and which would make chunking impossible offline — in a
unit test, in CI, on a developer machine with no credentials. The local alternative is a
SentencePiece model file plus the `sentencepiece` package, in the one package ADR 0001
established must stay import-cheap because every other package depends on it.

Meanwhile the chunker is on the critical path and S6 is not yet written.

## Options

1. **Add `sentencepiece` and a tokeniser model to `packages/core` now.** Rejected. It
   contradicts ADR 0001 directly, forces a model file and a native dependency onto
   `connectors`, `evals` and every other consumer that never embeds anything, and buys
   accuracy that nothing downstream can use until S6 exists to consume it.
2. **Call the Gemini API from the chunker.** Rejected outright. Network IO inside a pure
   splitting function makes the unit tests require credentials, makes chunking
   non-deterministic under retry, and puts a per-boundary round trip on the ingestion
   path.
3. **Hard-code a character heuristic and call the result `token_count`.** Rejected, and
   this is the one worth naming. It would work, and it would quietly write an estimate
   into a column every later measurement reads as fact. §4.11's prohibition on faking is
   usually read as a UI rule; a number in a database that is presented as one thing and
   computed as another is the same defect with a longer fuse.
4. **Inject the counter; ship a documented estimate as the default.** Accepted.

## Decision

`chunk_document` takes a keyword-only `count_tokens: TokenCounter`, defaulting to
`estimate_tokens`. The chunker's contract is stated against the counter it was given:
*no chunk exceeds `target_tokens` as measured by that counter*. That is exactly true for
any counter, so the chunker is correct today and stays correct when S6 replaces the
default.

`TokenCounter` takes its argument **positional-only**. Without the slash the protocol
demands a parameter literally named `text`, which rejects a `lambda`, a `partial`, and any
tokeniser wrapper that calls its argument something else — most of the callers S6 is
likely to have.

### CORRECTION (2026-08-27, S6): the conservatism claim below is false

Measured against the real tokenizer, `estimate_tokens` **under-counts** on two realistic
cases: masked text carrying `[EMAIL_A7]`-style pseudonyms (ratio 0.75) and quoted replies
(0.95). It under-counts worst on masked text, which is exactly what gets embedded.

The behaviour is unchanged and remains safe at the shipped defaults: 768 target x 0.75
worst ratio lands near 1024 real tokens against a 2048 provider limit, so the headroom
absorbs the error entirely. But it is the **headroom** that makes it safe, not the
conservatism, and the paragraph below invited someone to raise `target_tokens` toward the
limit on the strength of a guarantee that does not hold. S6 adds `truncated=true`
rejection as the backstop. See ADR 0009 for the measurements.

### `estimate_tokens` is an estimate, and says so everywhere

Four ASCII characters per token, plus **one token per non-ASCII character**. The second
half is the part that matters: a flat characters-over-four rule prices a Devanagari word
at a quarter of what a subword tokeniser charges, and under-counting is the asymmetric
error. An over-count yields chunks smaller than they could be and costs a little recall.
An under-count yields chunks the embedding model rejects — at request time, in a worker,
against a corpus that has already been masked and split. So the estimator is deliberately
conservative in the safe direction.

Its docstring, the module docstring and this ADR all state that it is an estimate.
Nothing may report `Chunk.token_count` as an exact model token count while it is in use.

## Consequences

- **`Chunk.token_count` is an estimate until S6 injects the real counter.** No chunk is
  persisted before then — chunks first reach Postgres in S8 — so no estimated value ever
  becomes a stored fact, provided S6 lands first. If that ordering changes, persistence
  must be gated until the real counter is available.
- Re-chunking with the real counter will produce different boundaries. Under §4.4 that is
  a **supersede**, not an overwrite, and S8 has to treat it as one.
- Wiring the real tokeniser at S6 will add a dependency, and possibly a model file, to the
  worker image. Flagged here so it is not discovered during a deploy.
- The chunker's own test suite drives boundary decisions through stub counters — including
  a deliberately super-additive one — so its correctness does not rest on the estimator's
  accuracy at all.

## One thing the counter made visible

The token budget is what forces a hard split, and a hard split is the only boundary that
can land anywhere. Sweeping budgets over non-Latin text showed it landing *inside*
grapheme clusters: a chunk beginning with a lone Devanagari virama, zero-width joiner or
variation selector. The offsets stayed correct, so nothing downstream failed — the text
was simply a fragment of a character, in the copy the model reads.

Cuts now move backwards off the inside of a cluster, using `unicodedata.combining` plus
the joiner, variation selectors, skin-tone modifiers, the keycap mark and regional
indicator pairs. It gives up only when a single cluster costs more than the whole budget,
where no cut both advances and keeps the cluster whole; that fallback is asserted rather
than left implicit. Span safety still takes precedence over cluster safety, because a
token split in half corrupts the offset map while a split cluster only looks wrong.

## Also decided in this slice

Two scope questions §9.2 raises that are answered here rather than in a second ADR,
because both are "what does this requirement mean when the layer it depends on does not
exist yet".

**"Never split across a decision statement."** Identifying a decision requires the
extraction layer of §10. S5 implements the reachable half: sentence boundaries are
preserved, and a sentence is split only when that sentence alone exceeds the hard limit.
A decision statement written as a sentence therefore survives intact. True decision
identification belongs to knowledge extraction and is explicitly not attempted here.

**Overlap is a budget, not a quota.** `overlap_ratio` is taken in whole segments, so a
document whose sentences each cost more than `target_tokens * overlap_ratio` gets no
overlap rather than a split sentence. Manufacturing an overlap by cutting a sentence in
half would break the guarantee above, and repeating a segment far larger than the budget
would spend most of the next chunk restating the previous one. At the spec's defaults the
budget is 115 tokens and ordinary prose runs 13 to 20 tokens a sentence, so several fit;
it is small targets that see none, which is also where repeating a whole sentence would
cost the most. `test_no_overlap_when_no_whole_segment_fits_the_budget` records this so it
is a documented rule rather than a surprise found at eval time.
