"""Enron corpus sampling — complete threads only (spec §19).

§19 is unusually direct about this one: *sample complete threads, never random messages*.
Random sampling shreds the reply graph, entity resolution is then resolving against
nothing, and the symptom appears in week five in a component that looks unrelated. So
this module never emits a fragment of a conversation. A thread is taken whole or not at
all, and the invariant is asserted rather than assumed.

**The manifest is byte-identical for the same corpus and seed.** That is a §19
requirement and it constrains the implementation everywhere: every list is sorted, the
custodian ranking breaks ties by name, the thread order is a seeded shuffle of a sorted
list, and **the manifest carries no timestamp** — a `generated_at` field would make every
run differ and quietly satisfy nobody.

Three deliberate departures from §19's sketch, each recorded in ADR 0008:

  * `sample_enron` **returns** the manifest instead of writing it. A function that
    returns its result is testable; one that writes a file as a side effect needs a
    temporary directory to assert anything. The caller writes it.
  * It returns identifiers rather than documents, and `load_documents` streams the
    bodies. Selection reads headers only, which is what makes a 500k-message corpus
    tractable; materialising every document to choose 50k of them would not be.
  * **Nothing here touches Postgres.** Persistence is S8, and `make seed` still says so.
"""

from __future__ import annotations

import json
import os
import random
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final

from jutsu_core import AclEntry, RawDocument, SourceSystem

from jutsu_connectors.local import LocalConnector, PathEscape, is_file, os_path, read_bytes
from jutsu_connectors.rfc822 import (
    UnparsableMessage,
    parse_message,
    to_raw_document,
    utc_from_timestamp,
)
from jutsu_connectors.threads import MessageLinks, ThreadIndex, build_thread_index

__all__ = [
    "DEFAULT_CUSTODIAN_COUNT",
    "DEFAULT_SEED",
    "DEFAULT_TARGET_MESSAGES",
    "MANIFEST_VERSION",
    "CustodianStat",
    "EmptyCorpus",
    "ManifestConnector",
    "SampleManifest",
    "SampleResult",
    "UnparsableManifest",
    "custodian_of",
    "load_documents",
    "sample_enron",
]

#: §19's values. Named so a caller can reference them rather than repeat the literals.
DEFAULT_SEED: Final = 20260819
DEFAULT_TARGET_MESSAGES: Final = 50_000
DEFAULT_CUSTODIAN_COUNT: Final = 150

#: Manifest format version. A change to what the manifest contains changes every
#: downstream comparison, so it is versioned rather than silently reshaped.
#:
#: **2** adds `message_threads`, without which a manifest cannot reproduce the canonical
#: thread ids and an ingest driven from it rebuilds the reply graph from each message in
#: isolation (§19).
MANIFEST_VERSION: Final = 2


def custodian_of(external_id: str) -> str:
    """Whose mailbox a message came from.

    The Enron corpus is `maildir/<custodian>/<folder>/<n>`, so the first path segment is
    the custodian. A message at the root of the corpus has no custodian and gets the
    empty string, which sorts first and is never selected — a file somebody dropped in
    the top of the directory is not a person's mailbox.
    """
    parts = PurePosixPath(external_id).parts
    return parts[0] if len(parts) > 1 else ""


@dataclass(frozen=True, slots=True)
class CustodianStat:
    name: str
    message_count: int


@dataclass(frozen=True, slots=True)
class SampleManifest:
    """What was sampled, and enough to reproduce it.

    Carries **no timestamp**, deliberately. §19 requires the same seed to produce a
    byte-identical manifest, and a generation time is the one field guaranteed to break
    that on every run.
    """

    version: int
    seed: int
    target_messages: int
    custodian_count: int
    corpus_messages: int
    corpus_unparsable: int
    sampled_messages: int
    sampled_threads: int
    custodians: tuple[CustodianStat, ...]
    thread_sizes: tuple[tuple[str, int], ...]
    message_ids: tuple[str, ...]
    #: `(external_id, thread_id)` for every sampled message, sorted by external id.
    #:
    #: The manifest exists to reproduce a sample, and until this field it could not
    #: reproduce the one property the sample is *for*. `LocalConnector.fetch` derives a
    #: thread from the message alone — its first `References` header, or its own id —
    #: which its docstring says is "best-effort" and "wrong for a corpus". The canonical
    #: thread comes from `build_thread_index`, which has seen every message, and it only
    #: exists while the whole-corpus index is in memory. Ingesting from a manifest
    #: without it would sample complete threads and then reassemble them incorrectly,
    #: which is the §19 failure with an extra step.
    message_threads: tuple[tuple[str, str], ...] = ()

    def to_json(self) -> str:
        """Canonical JSON. Sorted keys, fixed indent, no trailing whitespace.

        `ensure_ascii=False` so a non-ASCII custodian name survives as itself rather than
        as an escape — the file is meant to be read, and the bytes are still stable
        because the encoding is fixed at UTF-8 by whoever writes it.
        """
        payload = {
            "version": self.version,
            "seed": self.seed,
            "target_messages": self.target_messages,
            "custodian_count": self.custodian_count,
            "corpus_messages": self.corpus_messages,
            "corpus_unparsable": self.corpus_unparsable,
            "sampled_messages": self.sampled_messages,
            "sampled_threads": self.sampled_threads,
            "custodians": [
                {"name": stat.name, "message_count": stat.message_count} for stat in self.custodians
            ],
            "threads": [{"thread_id": thread, "size": size} for thread, size in self.thread_sizes],
            "messages": list(self.message_ids),
            "message_threads": [
                {"external_id": external_id, "thread_id": thread}
                for external_id, thread in self.message_threads
            ],
        }
        return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"

    @staticmethod
    def from_json(document: str) -> SampleManifest:
        """Read a manifest back. Refuses a version it does not understand.

        Only the fields an ingest needs are reconstructed — the counts and the custodian
        statistics are provenance for a human, not inputs to the walk. A manifest older
        than `MANIFEST_VERSION` is refused rather than upgraded: version 1 has no
        `message_threads`, so ingesting from it would silently fall back to per-message
        thread guesses, which is the failure the field was added to prevent.
        """
        try:
            payload = json.loads(document)
        except json.JSONDecodeError as error:
            # Wrapped rather than propagated, so the worker classifies it as permanent.
            # A `JSONDecodeError` reaching `classify` falls through to INTERNAL, which is
            # retryable — and retrying a truncated file re-reads the same truncated file
            # until the job dead-letters.
            raise UnparsableManifest("the manifest is not valid JSON") from error
        if not isinstance(payload, dict):
            raise UnparsableManifest("the manifest is not a JSON object")

        version = payload.get("version")
        if version != MANIFEST_VERSION:
            raise UnparsableManifest(
                f"manifest version {version!r} is not {MANIFEST_VERSION}; re-run the sampler"
            )

        pairs = tuple(
            (str(entry["external_id"]), str(entry["thread_id"]))
            for entry in payload.get("message_threads", [])
        )
        message_ids = tuple(str(value) for value in payload.get("messages", []))
        if len(pairs) != len(message_ids):
            raise UnparsableManifest(
                "manifest lists a different number of messages and thread assignments"
            )

        return SampleManifest(
            version=version,
            seed=int(payload["seed"]),
            target_messages=int(payload["target_messages"]),
            custodian_count=int(payload["custodian_count"]),
            corpus_messages=int(payload["corpus_messages"]),
            corpus_unparsable=int(payload["corpus_unparsable"]),
            sampled_messages=int(payload["sampled_messages"]),
            sampled_threads=int(payload["sampled_threads"]),
            custodians=tuple(
                CustodianStat(name=str(entry["name"]), message_count=int(entry["message_count"]))
                for entry in payload.get("custodians", [])
            ),
            thread_sizes=tuple(
                (str(entry["thread_id"]), int(entry["size"]))
                for entry in payload.get("threads", [])
            ),
            message_ids=message_ids,
            message_threads=pairs,
        )


@dataclass(frozen=True, slots=True)
class SampleResult:
    manifest: SampleManifest
    #: Corpus-relative identifiers, sorted. Feed to `load_documents`.
    external_ids: tuple[str, ...]
    #: The whole-corpus index, so a caller can ask what else is in a thread.
    threads: ThreadIndex


async def _scan(connector: LocalConnector) -> tuple[list[MessageLinks], dict[str, str], int]:
    """One pass over the corpus reading headers only.

    Headers only is the whole point: selection needs `Message-ID`, `References` and the
    custodian, and nothing else. Parsing bodies for half a million messages to choose
    fifty thousand of them is the difference between a minute and an afternoon.

    Unparsable files are counted and skipped. A corpus directory holds READMEs and
    truncated downloads, and one of them must not stop a sample.
    """
    links: list[MessageLinks] = []
    custodians: dict[str, str] = {}
    unparsable = 0

    async for external_id in connector.list_since(None):
        try:
            parsed = parse_message(read_bytes(connector.resolve(external_id)))
        except (UnparsableMessage, OSError, PathEscape):
            unparsable += 1
            continue

        links.append(
            MessageLinks(
                key=external_id,
                message_id=parsed.message_id,
                references=parsed.references,
            )
        )
        custodians[external_id] = custodian_of(external_id)

    return links, custodians, unparsable


def _rank_custodians(custodians: dict[str, str], limit: int) -> tuple[CustodianStat, ...]:
    """Custodians by mailbox size, largest first, ties broken by name.

    §19 says "select custodians by mailbox size" and stops there. Ties are broken
    alphabetically because two custodians with the same message count are otherwise
    ordered by whatever `Counter` felt like, and the manifest would differ between runs
    on a corpus where that happens — which, with hundreds of small mailboxes, it does.
    """
    counts = Counter(name for name in custodians.values() if name)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(CustodianStat(name=name, message_count=count) for name, count in ranked[:limit])


async def sample_enron(
    root: Path,
    *,
    seed: int = DEFAULT_SEED,
    target_messages: int = DEFAULT_TARGET_MESSAGES,
    custodian_count: int = DEFAULT_CUSTODIAN_COUNT,
) -> SampleResult:
    """Select complete threads involving the largest mailboxes, up to a message budget.

    **A thread that does not fit stops the sample.** The alternative — skipping it and
    continuing with smaller threads — packs the budget more tightly and biases the sample
    toward short conversations, which is precisely the property the corpus is being
    sampled for. Stopping undershoots the target visibly, and the manifest records both
    numbers so the shortfall is a fact rather than a surprise.

    Truncating a thread to fit is not an option and never will be (§19).
    """
    if target_messages < 1:
        raise ValueError("target_messages must be at least 1")
    if custodian_count < 1:
        raise ValueError("custodian_count must be at least 1")

    connector = LocalConnector(root)
    links, custodian_by_id, unparsable = await _scan(connector)

    # A corpus that yields nothing readable is a broken corpus, not an empty sample.
    #
    # This exists because the alternative happened. On Windows every Enron filename ends
    # in a dot, the Win32 path parser stripped it, and all 517 401 messages failed to
    # open — and the run *succeeded*: exit 0, a cheerful "sample manifest: …", and a
    # syntactically valid manifest describing zero messages. Had the pilot been scripted
    # straight through, it would have ingested nothing and reported success. §4.11 is
    # about not faking unfinished work; this is the same rule applied to a corpus that
    # is not there.
    #
    # `corpus_unparsable` is carried in the message because it is the diagnosis: a large
    # count with zero parsed is a *reading* failure (permissions, path handling, a
    # truncated download), while zero of both is a directory with no mail in it.
    if not links:
        raise EmptyCorpus(
            f"no parsable messages under {root} — {unparsable} file(s) could not be read "
            f"or parsed. A sample of nothing is not an empty sample, it is a corpus that "
            f"did not load."
        )

    index = build_thread_index(links)

    selected_custodians = _rank_custodians(custodian_by_id, custodian_count)
    selected_names = {stat.name for stat in selected_custodians}

    # A thread qualifies if any message in it sits in a selected custodian's mailbox.
    # Membership is a property of the whole thread, so a reply from an unselected
    # custodian comes along with it — which is the point of sampling by thread.
    qualifying = sorted(
        thread
        for thread in index.thread_ids
        if any(custodian_by_id.get(key, "") in selected_names for key in index.members(thread))
    )

    # Seeded shuffle of a *sorted* list. Sorting first is what makes the seed sufficient:
    # `random.shuffle` on a list whose order already varied would vary with it.
    order = list(qualifying)
    # S311 flags `random` as unsuitable for cryptography, and it is right that it is —
    # which is exactly why it is used here. §19 requires the same seed to reproduce a
    # byte-identical manifest, so the generator must be *reproducible*. A CSPRNG cannot
    # be seeded to repeat and would make the requirement unsatisfiable. Nothing about
    # this choice is security-relevant: it orders public threads for sampling.
    random.Random(seed).shuffle(order)  # noqa: S311

    chosen: list[str] = []
    thread_sizes: list[tuple[str, int]] = []
    total = 0
    for thread in order:
        members = index.members(thread)
        if total + len(members) > target_messages:
            break
        chosen.extend(members)
        thread_sizes.append((thread, len(members)))
        total += len(members)

    external_ids = tuple(sorted(chosen))
    manifest = SampleManifest(
        version=MANIFEST_VERSION,
        seed=seed,
        target_messages=target_messages,
        custodian_count=custodian_count,
        corpus_messages=len(links),
        corpus_unparsable=unparsable,
        sampled_messages=len(external_ids),
        sampled_threads=len(thread_sizes),
        custodians=selected_custodians,
        thread_sizes=tuple(sorted(thread_sizes)),
        message_ids=external_ids,
        message_threads=tuple((key, index.thread_of(key)) for key in external_ids),
    )
    return SampleResult(manifest=manifest, external_ids=external_ids, threads=index)


async def load_documents(
    root: Path, external_ids: Sequence[str], threads: ThreadIndex | None = None
) -> AsyncIterator[RawDocument]:
    """Stream the selected messages as documents, in the order given.

    `threads` supplies the canonical thread id from the whole-corpus index. Without it
    each document falls back to `LocalConnector.fetch`'s best-effort local guess, which is
    correct for a single document and wrong for a corpus — the two disagree by design and
    the caller chooses which it wants.
    """
    connector = LocalConnector(root)
    for external_id in external_ids:
        path = connector.resolve(external_id)
        try:
            parsed = parse_message(read_bytes(path))
        except (UnparsableMessage, OSError):
            # Selected from a header scan, unreadable now: the corpus changed underneath
            # the sample. Skipped rather than raised, so one vanished file cannot void a
            # sample of fifty thousand.
            continue

        thread_id = (
            threads.thread_of(external_id)
            if threads is not None
            else (parsed.references[0] if parsed.references else parsed.message_id)
        )
        yield to_raw_document(
            parsed,
            external_id=external_id,
            source_system=connector.system,
            uri=external_id,
            thread_id=thread_id,
            fallback_sent_at=utc_from_timestamp(os.stat(os_path(path)).st_mtime),
        )


class EmptyCorpus(RuntimeError):
    """A corpus from which not one message could be read.

    Its own class so a caller can tell "this directory has no mail" from "this file is
    not mail". The first is fatal to a sample; the second is routine and counted.
    """


class UnparsableManifest(RuntimeError):
    """A sample manifest that cannot be used to drive an ingest.

    Its own class so the worker can classify it as permanent: re-reading the same file
    produces the same answer, and the fix is to re-run the sampler, not to retry.
    """


class ManifestConnector:
    """A `LocalConnector` restricted to one sample, with the canonical thread ids.

    This is what puts §19 on the ingestion path. `make seed --root <maildir>` walks a
    directory; that is a *directory walk*, and on the real corpus it is precisely the
    "random messages" §19 forbids — it takes whatever it finds up to `--max-documents`,
    which shreds the reply graph and leaves entity resolution with nothing to resolve.

    Driving the walk from a manifest instead means the set of documents is the set the
    sampler chose: complete threads, seeded, reproducible. Two properties follow, and
    both matter more than they look:

      * **`list_since` yields the manifest's identifiers, not the corpus's.** A message
        that is in the corpus but not in the sample is never listed, so it never gets a
        job, so `--max-documents` stops bounding *which* documents arrive and only bounds
        how many of the chosen ones are processed per run.
      * **`fetch` overrides the thread id.** `LocalConnector.fetch` says in its own
        docstring that its thread is best-effort and that the corpus knows better. The
        corpus's answer lives in the manifest, and using it here is the difference
        between reconstructing the threads that were sampled and reconstructing
        something else.

    Everything else — parsing, containment, ACL derivation — is `LocalConnector`,
    unchanged and not re-implemented.
    """

    system = SourceSystem.LOCAL

    __slots__ = ("_ids", "_local", "_threads")

    def __init__(self, root: Path, manifest: SampleManifest) -> None:
        self._local = LocalConnector(root)
        self._ids = manifest.message_ids
        self._threads = dict(manifest.message_threads)

    @property
    def root(self) -> Path:
        return self._local.root

    def resolve(self, external_id: str) -> Path:
        return self._local.resolve(external_id)

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        """The sampled identifiers, in manifest order, filtered by the cursor.

        Same cursor semantics as `LocalConnector`: an ISO-8601 instant compared against
        the file's modification time, so a re-run lists only what changed. An identifier
        whose file has since vanished is skipped rather than raised — a sample of fifty
        thousand must not be voided by one deleted file, which is the rule
        `load_documents` already follows.
        """
        since = datetime.fromisoformat(cursor) if cursor else None

        for external_id in self._ids:
            try:
                path = self._local.resolve(external_id)
                if (
                    since is not None
                    and utc_from_timestamp(os.stat(os_path(path)).st_mtime) < since
                ):
                    continue
                if not is_file(path):
                    continue
            except (OSError, PathEscape):
                continue
            yield external_id

    async def fetch(self, external_id: str) -> RawDocument:
        """The document, carrying the thread the whole-corpus index assigned it."""
        document = await self._local.fetch(external_id)
        thread_id = self._threads.get(external_id)
        if thread_id is None:
            # In the corpus and resolvable, but not in this sample. Refused rather than
            # ingested with a guessed thread: a document nobody sampled has no business
            # arriving through a sampled source.
            raise UnparsableManifest(f"{external_id} is not in this sample")
        return document.model_copy(update={"thread_id": thread_id})

    async def acls(self, external_id: str) -> list[AclEntry]:
        return await self._local.acls(external_id)
