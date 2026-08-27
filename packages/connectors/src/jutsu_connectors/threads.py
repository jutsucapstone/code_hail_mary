"""Thread reconstruction across a corpus (spec §19).

**Random-sampling a mail corpus destroys the reply graph.** That is the trap CLAUDE.md
names, and this module is what makes avoiding it possible: entity resolution has nothing
to resolve if half of every conversation is missing, and week five is when you find out.
So the sampler works in threads, and a thread is what this computes.

The algorithm is a union-find over message ids, and the choice matters:

  * **`References` is a hint, not a chain.** Clients truncate it from the middle once it
    grows, so following it as a linked list loses the head of long conversations.
    Unioning every id that appears anywhere keeps the component joined through whatever
    survived the truncation.
  * **Cycles cannot break it.** `A` references `B` while `B` references `A` happens in
    real corpora — forwarded loops, broken clients — and a parent-pointer walk would spin
    forever. Union-find is indifferent to them.
  * **A missing parent still joins its children.** A reply to a message outside the
    sample unions through the *referenced id* even though no message carries it, so two
    replies to an absent original land in one thread rather than two. Referenced-but-
    absent ids are first-class members of the component for exactly this reason.

The thread id is the lexicographically smallest id in the component, including absent
ones. It is a **canonical label, not "the first message"** — dates are missing or wrong
often enough that ordering by them would make thread identity depend on the least
reliable header in the file.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

__all__ = ["MessageLinks", "ThreadIndex", "build_thread_index"]


@dataclass(frozen=True, slots=True)
class MessageLinks:
    """What one message says about its place in a conversation.

    `key` is how the caller identifies the message — for a file corpus, its path. It is
    kept separate from `message_id` because a message may have no `Message-ID` at all,
    and it still has to be threadable and retrievable.
    """

    key: str
    message_id: str | None
    references: tuple[str, ...]


class ThreadIndex:
    """Which thread each message belongs to, over one corpus.

    Built once by `build_thread_index` and then read. Nothing mutates it afterwards:
    thread identity that changed as more of a corpus was read would make sampling
    non-deterministic, which §19 forbids.
    """

    __slots__ = ("_members", "_thread_of")

    def __init__(self, thread_of: Mapping[str, str], members: Mapping[str, tuple[str, ...]]):
        self._thread_of = dict(thread_of)
        self._members = {thread: tuple(keys) for thread, keys in members.items()}

    def thread_of(self, key: str) -> str:
        """The thread id for one message key."""
        return self._thread_of[key]

    def members(self, thread_id: str) -> tuple[str, ...]:
        """Every message key in a thread, sorted. **This is the complete thread.**

        Sorted rather than in discovery order, so a sample built from it is byte-identical
        across runs regardless of the order the corpus was walked in.
        """
        return self._members[thread_id]

    @property
    def thread_ids(self) -> tuple[str, ...]:
        """Every thread, sorted. The deterministic starting point for sampling."""
        return tuple(sorted(self._members))

    def __len__(self) -> int:
        return len(self._members)


def _find(parents: dict[str, str], node: str) -> str:
    """Union-find with path compression, iterative.

    Iterative rather than recursive on purpose: a long reply chain in a real corpus is
    thousands of messages deep, and the recursive form hits Python's stack limit on a
    corpus that is otherwise perfectly valid.
    """
    root = node
    while parents[root] != root:
        root = parents[root]
    while parents[node] != root:
        parents[node], node = root, parents[node]
    return root


def _union(parents: dict[str, str], left: str, right: str) -> None:
    """Join two components, keeping the lexicographically smaller root.

    Not union-by-rank. Rank would give a balanced tree and an arbitrary root; here the
    root *is* the thread id, so it has to be a deterministic function of the members
    rather than of the order they happened to arrive in.
    """
    left_root, right_root = _find(parents, left), _find(parents, right)
    if left_root == right_root:
        return
    if right_root < left_root:
        left_root, right_root = right_root, left_root
    parents[right_root] = left_root


def build_thread_index(messages: Iterable[MessageLinks]) -> ThreadIndex:
    """Group a corpus into threads.

    A message with no `Message-ID` and no references is its own thread, keyed on its
    corpus key. That is the honest answer — there is nothing to join it to — and it keeps
    such messages sampleable rather than silently dropped.
    """
    parents: dict[str, str] = {}
    # Corpus key -> the id used to represent it in the union-find. A message with no
    # Message-ID is represented by its own key, namespaced so it cannot collide with a
    # real id that happens to look like a path.
    representative: dict[str, str] = {}

    def ensure(node: str) -> None:
        parents.setdefault(node, node)

    materialised = list(messages)

    for message in materialised:
        own = message.message_id or f"jutsu-keyed:{message.key}"
        representative[message.key] = own
        ensure(own)
        for reference in message.references:
            # Referenced ids are added even when no message in the corpus carries them.
            # Two replies to a message outside the sample must still share a thread.
            ensure(reference)
            _union(parents, own, reference)

    thread_of: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    for message in materialised:
        thread = _find(parents, representative[message.key])
        thread_of[message.key] = thread
        members.setdefault(thread, []).append(message.key)

    return ThreadIndex(thread_of, {thread: tuple(sorted(keys)) for thread, keys in members.items()})
