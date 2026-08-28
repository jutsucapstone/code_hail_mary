"""Complete-thread sampling and manifest reproducibility (§19).

Two properties carry this module, and both are named in the spec:

  * **Threads are never truncated.** Every sampled thread is present in full, or absent.
    Random-sampling messages shreds the reply graph and entity resolution has nothing to
    resolve — the failure surfaces in week five, in a component that looks unrelated.
  * **The same corpus and seed produce a byte-identical manifest.** Asserted on the bytes,
    not on the object, because that is what the spec asks for and what a reviewer can
    check with `diff`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from corpus_support import THREADED_CORPUS, Message, write_corpus
from jutsu_connectors.enron import (
    DEFAULT_CUSTODIAN_COUNT,
    DEFAULT_SEED,
    DEFAULT_TARGET_MESSAGES,
    EmptyCorpus,
    custodian_of,
    load_documents,
    sample_enron,
)

CRLF = "\r\n"


def mail(path: str, message_id: str, *references: str, sender: str = "a@example.com") -> Message:
    headers = {
        "Message-ID": f"<{message_id}>",
        "Date": "Mon, 12 Jan 2026 09:15:00 -0600",
        "From": sender,
        "To": "b@example.com",
        "Subject": "generated",
    }
    if references:
        headers["References"] = " ".join(f"<{reference}>" for reference in references)
    return Message(path=path, headers=headers, body=f"body of {message_id}")


def wide_corpus(root: Path) -> Path:
    """Four custodians of very different sizes, with multi-message threads.

    `big` has the most messages, `tiny` the fewest, and `even_a`/`even_b` are deliberately
    the same size so the tie-break is exercised rather than assumed.
    """
    messages: list[Message] = []
    for index in range(12):
        root_id = f"big{index}@x"
        messages.append(mail(f"big/inbox/{index}a", root_id))
        messages.append(mail(f"big/inbox/{index}b", f"big{index}r@x", root_id))
    for index in range(5):
        messages.append(mail(f"even_b/inbox/{index}", f"eb{index}@x"))
    for index in range(5):
        messages.append(mail(f"even_a/inbox/{index}", f"ea{index}@x"))
    messages.append(mail("tiny/inbox/0", "tiny0@x"))
    return write_corpus(root, messages)


@pytest.fixture(name="corpus")
def corpus_fixture(tmp_path: Path) -> Path:
    return write_corpus(tmp_path / "maildir", THREADED_CORPUS)


class TestCustodian:
    @pytest.mark.parametrize(
        ("identifier", "expected"),
        [
            ("allen/sent/1", "allen"),
            ("allen/inbox/deep/9", "allen"),
            ("loose-file", ""),
            ("", ""),
        ],
    )
    def test_the_first_segment_is_the_custodian(self, identifier: str, expected: str) -> None:
        assert custodian_of(identifier) == expected


class TestCompleteThreads:
    async def test_no_thread_is_ever_truncated(self, corpus: Path) -> None:
        """The invariant §19 names. Every member of a sampled thread is in the sample."""
        result = await sample_enron(corpus, target_messages=100, custodian_count=10)
        sampled = set(result.external_ids)

        for external_id in sampled:
            thread = result.threads.thread_of(external_id)
            assert set(result.threads.members(thread)) <= sampled, f"thread {thread} was truncated"

    async def test_a_thread_pulls_in_the_other_custodians_messages(self, corpus: Path) -> None:
        """Selecting one mailbox brings the whole conversation, including replies filed
        in someone else's. That is the entire point of sampling by thread."""
        result = await sample_enron(corpus, target_messages=100, custodian_count=1)
        assert "allen/sent/1" in result.external_ids
        assert "taylor/inbox/1" in result.external_ids

    async def test_a_thread_that_does_not_fit_stops_the_sample(self, tmp_path: Path) -> None:
        """Skipping it and continuing would bias the sample toward short threads, which is
        the property the corpus is being sampled for. Stopping undershoots visibly."""
        root = wide_corpus(tmp_path / "maildir")
        result = await sample_enron(root, target_messages=3, custodian_count=10)

        assert result.manifest.sampled_messages <= 3
        for external_id in result.external_ids:
            thread = result.threads.thread_of(external_id)
            assert set(result.threads.members(thread)) <= set(result.external_ids)

    async def test_the_budget_is_never_exceeded(self, tmp_path: Path) -> None:
        root = wide_corpus(tmp_path / "maildir")
        for target in (1, 2, 5, 9, 20, 100):
            result = await sample_enron(root, target_messages=target, custodian_count=10)
            assert result.manifest.sampled_messages <= target, target

    async def test_the_manifest_counts_match_the_selection(self, corpus: Path) -> None:
        result = await sample_enron(corpus, target_messages=100, custodian_count=10)
        assert result.manifest.sampled_messages == len(result.external_ids)
        assert result.manifest.sampled_threads == len(result.manifest.thread_sizes)
        assert sum(size for _thread, size in result.manifest.thread_sizes) == len(
            result.external_ids
        )


class TestDeterminism:
    async def test_the_same_seed_gives_the_same_selection(self, tmp_path: Path) -> None:
        root = wide_corpus(tmp_path / "maildir")
        one = await sample_enron(root, seed=7, target_messages=20, custodian_count=3)
        two = await sample_enron(root, seed=7, target_messages=20, custodian_count=3)
        assert one.external_ids == two.external_ids

    async def test_the_manifest_is_byte_identical(self, tmp_path: Path) -> None:
        """§19's actual requirement, asserted on bytes rather than on the object."""
        root = wide_corpus(tmp_path / "maildir")
        one = await sample_enron(root, seed=7, target_messages=20, custodian_count=3)
        two = await sample_enron(root, seed=7, target_messages=20, custodian_count=3)
        assert one.manifest.to_json().encode("utf-8") == two.manifest.to_json().encode("utf-8")

    async def test_a_different_seed_changes_the_selection(self, tmp_path: Path) -> None:
        """Otherwise the seed is decoration and the sample is not a sample."""
        root = wide_corpus(tmp_path / "maildir")
        selections = {
            (await sample_enron(root, seed=seed, target_messages=8, custodian_count=4)).external_ids
            for seed in range(6)
        }
        assert len(selections) > 1

    async def test_the_manifest_carries_no_timestamp(self, tmp_path: Path) -> None:
        """A `generated_at` field would break byte identity on every single run."""
        root = wide_corpus(tmp_path / "maildir")
        payload = json.loads(
            (
                await sample_enron(root, seed=1, target_messages=10, custodian_count=2)
            ).manifest.to_json()
        )
        for key in payload:
            assert "time" not in key.lower()
            assert "date" not in key.lower()
            assert key != "generated_at"

    async def test_the_manifest_is_valid_sorted_json(self, tmp_path: Path) -> None:
        root = wide_corpus(tmp_path / "maildir")
        text = (
            await sample_enron(root, seed=1, target_messages=10, custodian_count=2)
        ).manifest.to_json()
        assert text.endswith("\n")
        payload = json.loads(text)
        assert list(payload) == sorted(payload)
        assert payload["messages"] == sorted(payload["messages"])

    async def test_selection_does_not_depend_on_walk_order(self, tmp_path: Path) -> None:
        """Two corpora with the same content in different file order sample identically."""
        first = wide_corpus(tmp_path / "one")
        second = wide_corpus(tmp_path / "two")
        one = await sample_enron(first, seed=3, target_messages=15, custodian_count=3)
        two = await sample_enron(second, seed=3, target_messages=15, custodian_count=3)
        assert one.external_ids == two.external_ids


class TestCustodianSelection:
    async def test_custodians_are_ranked_by_mailbox_size(self, tmp_path: Path) -> None:
        root = wide_corpus(tmp_path / "maildir")
        result = await sample_enron(root, target_messages=1000, custodian_count=4)
        names = [stat.name for stat in result.manifest.custodians]
        assert names[0] == "big"
        assert names[-1] == "tiny"

    async def test_ties_are_broken_by_name(self, tmp_path: Path) -> None:
        """Two custodians of equal size otherwise order by whatever `Counter` felt like,
        and the manifest differs between runs on a corpus where that happens."""
        root = wide_corpus(tmp_path / "maildir")
        result = await sample_enron(root, target_messages=1000, custodian_count=4)
        names = [stat.name for stat in result.manifest.custodians]
        assert names.index("even_a") < names.index("even_b")

    async def test_only_the_requested_number_are_selected(self, tmp_path: Path) -> None:
        root = wide_corpus(tmp_path / "maildir")
        result = await sample_enron(root, target_messages=1000, custodian_count=2)
        assert len(result.manifest.custodians) == 2

    async def test_a_file_at_the_corpus_root_has_no_custodian(self, tmp_path: Path) -> None:
        root = wide_corpus(tmp_path / "maildir")
        (root / "loose").write_bytes(
            f"Message-ID: <loose@x>{CRLF}From: a@example.com{CRLF}{CRLF}body".encode()
        )
        result = await sample_enron(root, target_messages=1000, custodian_count=10)
        assert all(stat.name for stat in result.manifest.custodians)
        assert "loose" not in result.external_ids


class TestCorpusHygiene:
    async def test_unparsable_files_are_counted_not_fatal(self, corpus: Path) -> None:
        (corpus / "allen" / "README").write_text("not mail", encoding="utf-8")
        (corpus / "allen" / "junk").write_bytes(bytes(range(256)))

        result = await sample_enron(corpus, target_messages=100, custodian_count=10)
        assert result.manifest.corpus_unparsable >= 2
        assert result.manifest.corpus_messages == len(THREADED_CORPUS)
        assert "allen/README" not in result.external_ids

    async def test_an_empty_corpus_is_refused(self, tmp_path: Path) -> None:
        """Behaviour change: this used to return an empty sample.

        It returned `external_ids == ()` and a manifest of zero messages, which is
        indistinguishable from a corpus that failed to *load* — and that is exactly what
        happened on the real corpus, where every one of 517 401 files was unreadable and
        the run still exited 0. A sample of nothing is now a refusal.
        """
        root = tmp_path / "empty"
        root.mkdir()
        with pytest.raises(EmptyCorpus, match="no parsable messages"):
            await sample_enron(root, target_messages=100, custodian_count=10)

    async def test_a_corpus_of_only_unreadable_files_is_refused(self, tmp_path: Path) -> None:
        """The shape the Windows trailing-dot defect produced: files found, none parsed."""
        root = tmp_path / "junk"
        (root / "allen").mkdir(parents=True)
        (root / "allen" / "README").write_text("not mail", encoding="utf-8")
        (root / "allen" / "binary").write_bytes(bytes(range(256)))

        with pytest.raises(EmptyCorpus) as caught:
            await sample_enron(root, target_messages=100, custodian_count=10)

        # The count is the diagnosis: files present but unreadable is a loading failure,
        # not an empty directory.
        assert "2 file(s)" in str(caught.value)

    async def test_the_refusal_names_the_corpus_root(self, tmp_path: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()
        with pytest.raises(EmptyCorpus, match=re.escape(str(root))):
            await sample_enron(root, target_messages=100, custodian_count=10)

    @pytest.mark.parametrize(("target", "custodians"), [(0, 5), (-1, 5), (5, 0), (5, -1)])
    async def test_impossible_parameters_are_refused(
        self, corpus: Path, target: int, custodians: int
    ) -> None:
        with pytest.raises(ValueError):
            await sample_enron(corpus, target_messages=target, custodian_count=custodians)

    def test_the_spec_defaults_are_the_signature_defaults(self) -> None:
        assert (DEFAULT_SEED, DEFAULT_TARGET_MESSAGES, DEFAULT_CUSTODIAN_COUNT) == (
            20260819,
            50_000,
            150,
        )


class TestLoadDocuments:
    async def test_it_yields_documents_in_the_order_given(self, corpus: Path) -> None:
        result = await sample_enron(corpus, target_messages=100, custodian_count=10)
        documents = [document async for document in load_documents(corpus, result.external_ids)]
        assert [document.external_id for document in documents] == list(result.external_ids)

    async def test_the_canonical_thread_id_comes_from_the_corpus_index(self, corpus: Path) -> None:
        """The whole-corpus answer, not `fetch`'s single-message best effort."""
        result = await sample_enron(corpus, target_messages=100, custodian_count=10)
        documents = {
            document.external_id: document
            async for document in load_documents(corpus, result.external_ids, result.threads)
        }
        thread = {documents[key].thread_id for key in ("allen/sent/1", "taylor/inbox/1")}
        assert len(thread) == 1

    async def test_a_file_removed_after_selection_is_skipped(self, corpus: Path) -> None:
        """The corpus changed underneath the sample. One vanished file must not void it."""
        result = await sample_enron(corpus, target_messages=100, custodian_count=10)
        (corpus / "allen" / "sent" / "1").unlink()

        documents = [document async for document in load_documents(corpus, result.external_ids)]
        assert len(documents) == len(result.external_ids) - 1

    async def test_every_document_carries_grants_and_provenance(self, corpus: Path) -> None:
        result = await sample_enron(corpus, target_messages=100, custodian_count=10)
        async for document in load_documents(corpus, result.external_ids, result.threads):
            assert document.acls, document.external_id
            assert document.uri == document.external_id
            assert document.content_hash
            assert document.created_at.tzinfo is not None
