"""Thread reconstruction (§19).

The trap this exists to avoid is named in CLAUDE.md: random-sampling the corpus destroys
the reply graph and entity resolution has nothing left to resolve. Every test here is
about one question — **does this message end up in the same thread as its conversation** —
under the conditions a real mail corpus actually presents: truncated `References`,
cycles, and parents that are not in the sample at all.
"""

from __future__ import annotations

from jutsu_connectors.threads import MessageLinks, build_thread_index


def links(key: str, message_id: str | None, *references: str) -> MessageLinks:
    return MessageLinks(key=key, message_id=message_id, references=tuple(references))


class TestBasicThreading:
    def test_a_lone_message_is_its_own_thread(self) -> None:
        index = build_thread_index([links("a", "a@x")])
        assert len(index) == 1
        assert index.members(index.thread_of("a")) == ("a",)

    def test_a_reply_joins_its_parent(self) -> None:
        index = build_thread_index([links("a", "a@x"), links("b", "b@x", "a@x")])
        assert index.thread_of("a") == index.thread_of("b")
        assert index.members(index.thread_of("a")) == ("a", "b")

    def test_a_chain_of_three_is_one_thread(self) -> None:
        index = build_thread_index(
            [links("a", "a@x"), links("b", "b@x", "a@x"), links("c", "c@x", "a@x", "b@x")]
        )
        assert len({index.thread_of(key) for key in ("a", "b", "c")}) == 1
        assert len(index) == 1

    def test_unrelated_messages_stay_apart(self) -> None:
        index = build_thread_index([links("a", "a@x"), links("b", "b@x")])
        assert index.thread_of("a") != index.thread_of("b")
        assert len(index) == 2

    def test_in_reply_to_alone_is_enough(self) -> None:
        """`References` is often absent; `In-Reply-To` is folded into it by the parser."""
        index = build_thread_index([links("a", "a@x"), links("b", "b@x", "a@x")])
        assert index.thread_of("a") == index.thread_of("b")


class TestRealCorpusConditions:
    def test_a_truncated_references_header_still_joins(self) -> None:
        """Clients drop ids from the middle of `References` once it grows.

        `c` names only the root and `d` names only the second message. Following the
        header as a linked list would leave them in different threads; unioning every id
        that appears keeps the conversation whole.
        """
        index = build_thread_index(
            [
                links("a", "a@x"),
                links("b", "b@x", "a@x"),
                links("c", "c@x", "a@x"),
                links("d", "d@x", "b@x"),
            ]
        )
        assert len({index.thread_of(key) for key in ("a", "b", "c", "d")}) == 1

    def test_a_circular_reference_terminates(self) -> None:
        """`A` references `B` while `B` references `A`. A parent walk spins forever."""
        index = build_thread_index([links("a", "a@x", "b@x"), links("b", "b@x", "a@x")])
        assert index.thread_of("a") == index.thread_of("b")
        assert len(index) == 1

    def test_a_self_reference_terminates(self) -> None:
        index = build_thread_index([links("a", "a@x", "a@x")])
        assert index.members(index.thread_of("a")) == ("a",)

    def test_two_replies_to_an_absent_parent_share_a_thread(self) -> None:
        """The message they answer is outside the sample, and they still belong together.

        Referenced-but-absent ids are members of the component precisely so this holds.
        """
        index = build_thread_index([links("b", "b@x", "missing@x"), links("c", "c@x", "missing@x")])
        assert index.thread_of("b") == index.thread_of("c")
        assert len(index) == 1

    def test_a_message_without_a_message_id_is_still_threadable(self) -> None:
        """A third of a real corpus has no `Message-ID`. It is still a document."""
        index = build_thread_index([links("a", None), links("b", None)])
        assert index.thread_of("a") != index.thread_of("b")
        assert len(index) == 2

    def test_a_message_without_an_id_can_still_join_by_reference(self) -> None:
        index = build_thread_index([links("a", "a@x"), links("b", None, "a@x")])
        assert index.thread_of("a") == index.thread_of("b")

    def test_a_synthetic_key_cannot_collide_with_a_real_id(self) -> None:
        """A message with no id is keyed on its path, namespaced so a path-shaped
        `Message-ID` elsewhere in the corpus cannot accidentally join it."""
        index = build_thread_index([links("allen/sent/1", None), links("b", "allen/sent/1")])
        assert index.thread_of("allen/sent/1") != index.thread_of("b")


class TestDeterminism:
    def test_thread_ids_do_not_depend_on_input_order(self) -> None:
        """Sampling determinism rests on this: the same corpus, any walk order, one answer."""
        forward = [links("a", "a@x"), links("b", "b@x", "a@x"), links("c", "c@x")]
        backward = list(reversed(forward))

        one, two = build_thread_index(forward), build_thread_index(backward)
        assert {key: one.thread_of(key) for key in ("a", "b", "c")} == {
            key: two.thread_of(key) for key in ("a", "b", "c")
        }
        assert one.thread_ids == two.thread_ids

    def test_members_are_sorted(self) -> None:
        index = build_thread_index(
            [links("z", "z@x", "root@x"), links("a", "a@x", "root@x"), links("m", "m@x", "root@x")]
        )
        assert index.members(index.thread_of("a")) == ("a", "m", "z")

    def test_the_thread_id_is_the_smallest_id_in_the_component(self) -> None:
        """A canonical label, not "the first message" — dates are the least reliable header."""
        index = build_thread_index([links("b", "zzz@x", "aaa@x"), links("a", "aaa@x")])
        assert index.thread_of("a") == "aaa@x"
        assert index.thread_of("b") == "aaa@x"

    def test_a_deep_chain_does_not_exhaust_the_stack(self) -> None:
        """Recursive union-find dies on a real long-running thread; this one is iterative."""
        chain = [links("m0", "m0@x")]
        chain.extend(links(f"m{i}", f"m{i}@x", f"m{i - 1}@x") for i in range(1, 4000))
        index = build_thread_index(chain)
        assert len(index) == 1
        assert len(index.members(index.thread_of("m3999"))) == 4000
