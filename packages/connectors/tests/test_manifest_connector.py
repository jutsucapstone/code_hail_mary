"""Ingesting a sample rather than a directory (§19).

`make seed --root <maildir>` walks a directory and takes what it meets up to
`--max-documents`. On a fixture that is fine; on the real corpus it is exactly the random
sampling §19 forbids, and the damage — a shredded reply graph — is invisible until entity
resolution has nothing to resolve.

`ManifestConnector` is the seam that fixes it. These tests assert the two properties that
make it worth having: the walk yields the *sampled* identifiers and nothing else, and the
documents carry the thread ids the whole-corpus index assigned rather than the per-message
guesses `LocalConnector.fetch` documents as "wrong for a corpus".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corpus_support import THREADED_CORPUS, write_corpus
from jutsu_connectors.enron import (
    MANIFEST_VERSION,
    ManifestConnector,
    SampleManifest,
    UnparsableManifest,
    sample_enron,
)
from jutsu_connectors.local import LocalConnector


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    return write_corpus(tmp_path / "maildir", THREADED_CORPUS)


async def _sampled(corpus: Path) -> tuple[SampleManifest, ManifestConnector]:
    result = await sample_enron(corpus, target_messages=100, custodian_count=10)
    manifest = SampleManifest.from_json(result.manifest.to_json())
    return manifest, ManifestConnector(corpus, manifest)


class TestTheManifestRoundTrips:
    async def test_it_survives_json(self, corpus: Path) -> None:
        result = await sample_enron(corpus, target_messages=100, custodian_count=10)
        restored = SampleManifest.from_json(result.manifest.to_json())
        assert restored.message_ids == result.manifest.message_ids
        assert restored.message_threads == result.manifest.message_threads
        assert restored.seed == result.manifest.seed

    async def test_it_carries_a_thread_for_every_sampled_message(self, corpus: Path) -> None:
        """The field the version bump exists for."""
        manifest, _ = await _sampled(corpus)
        assert manifest.message_threads
        assert len(manifest.message_threads) == len(manifest.message_ids)
        assert {key for key, _ in manifest.message_threads} == set(manifest.message_ids)

    async def test_it_is_still_byte_identical_for_a_seed(self, corpus: Path) -> None:
        """§19's requirement, unaffected by the new field."""
        one = await sample_enron(corpus, seed=7, target_messages=100, custodian_count=10)
        two = await sample_enron(corpus, seed=7, target_messages=100, custodian_count=10)
        assert one.manifest.to_json().encode("utf-8") == two.manifest.to_json().encode("utf-8")

    def test_an_older_version_is_refused_rather_than_upgraded(self) -> None:
        """Version 1 has no thread map, so ingesting from it would guess silently."""
        with pytest.raises(UnparsableManifest, match="version"):
            SampleManifest.from_json('{"version": 1, "messages": []}')

    def test_a_mismatched_thread_map_is_refused(self) -> None:
        document = (
            f'{{"version": {MANIFEST_VERSION}, "seed": 1, "target_messages": 1, '
            f'"custodian_count": 1, "corpus_messages": 1, "corpus_unparsable": 0, '
            f'"sampled_messages": 1, "sampled_threads": 1, "custodians": [], '
            f'"threads": [], "messages": ["a", "b"], "message_threads": []}}'
        )
        with pytest.raises(UnparsableManifest, match="different number"):
            SampleManifest.from_json(document)


class TestTheWalkIsTheSample:
    async def test_it_lists_exactly_the_sampled_identifiers(self, corpus: Path) -> None:
        manifest, connector = await _sampled(corpus)
        listed = [external_id async for external_id in connector.list_since(None)]
        assert listed == list(manifest.message_ids)

    async def test_it_lists_fewer_than_the_whole_corpus_when_the_budget_bites(
        self, corpus: Path
    ) -> None:
        """A sample that happened to be the whole corpus would prove nothing."""
        result = await sample_enron(corpus, target_messages=2, custodian_count=10)
        manifest = SampleManifest.from_json(result.manifest.to_json())
        connector = ManifestConnector(corpus, manifest)

        sampled = [external_id async for external_id in connector.list_since(None)]
        everything = [external_id async for external_id in LocalConnector(corpus).list_since(None)]
        assert len(sampled) < len(everything)
        assert set(sampled) <= set(everything)

    async def test_a_vanished_file_is_skipped_not_raised(self, corpus: Path) -> None:
        """One deleted message must not void a sample of fifty thousand."""
        manifest, connector = await _sampled(corpus)
        victim = manifest.message_ids[0]
        connector.resolve(victim).unlink()

        listed = [external_id async for external_id in connector.list_since(None)]
        assert victim not in listed
        assert len(listed) == len(manifest.message_ids) - 1


class TestTheThreadIsTheCorpusAnswer:
    async def test_fetch_uses_the_manifest_thread(self, corpus: Path) -> None:
        manifest, connector = await _sampled(corpus)
        expected = dict(manifest.message_threads)

        for external_id in manifest.message_ids:
            document = await connector.fetch(external_id)
            assert document.thread_id == expected[external_id]

    async def test_the_manifest_wins_when_the_two_disagree(self, corpus: Path) -> None:
        """The override is applied, not merely available.

        Asserted by handing the connector a manifest that says something the message
        itself does not, rather than by hunting for a fixture where the corpus index and
        the per-message guess happen to differ — on this fixture they agree everywhere,
        which is a fact about the fixture and not about the code. Substituting the thread
        makes the question unambiguous: if `fetch` returned the local guess, this fails.
        """
        manifest, _ = await _sampled(corpus)
        external_id = manifest.message_ids[0]
        local_answer = (await LocalConnector(corpus).fetch(external_id)).thread_id

        rewritten = SampleManifest.from_json(
            manifest.to_json().replace(
                f'"thread_id": "{dict(manifest.message_threads)[external_id]}"',
                '"thread_id": "<canonical-from-the-whole-corpus>"',
                1,
            )
        )
        document = await ManifestConnector(corpus, rewritten).fetch(external_id)

        assert document.thread_id == "<canonical-from-the-whole-corpus>"
        assert document.thread_id != local_answer

    async def test_nothing_else_about_the_document_changes(self, corpus: Path) -> None:
        """Only the thread is overridden — body, hash and grants stay the local ones."""
        manifest, connector = await _sampled(corpus)
        external_id = manifest.message_ids[0]

        sampled = await connector.fetch(external_id)
        local = await LocalConnector(corpus).fetch(external_id)

        assert sampled.body == local.body
        assert sampled.content_hash == local.content_hash
        assert sampled.external_id == local.external_id
        assert sampled.acls == local.acls

    async def test_a_document_outside_the_sample_is_refused(self, corpus: Path) -> None:
        """A document nobody sampled has no business arriving through a sampled source.

        Sampled with a deliberately small budget so there is always something outside it.
        The first version used the same generous budget as its neighbours and skipped
        when the sample turned out to cover the whole fixture — a conditional skip that
        asserted nothing, and one the gate correctly refused to tolerate in a coverage
        measurement.
        """
        result = await sample_enron(corpus, target_messages=2, custodian_count=10)
        manifest = SampleManifest.from_json(result.manifest.to_json())
        connector = ManifestConnector(corpus, manifest)

        everything = {e async for e in LocalConnector(corpus).list_since(None)}
        outside = sorted(everything - set(manifest.message_ids))
        assert outside, "the small-budget sample still covered the whole fixture corpus"

        with pytest.raises(UnparsableManifest, match="not in this sample"):
            await connector.fetch(outside[0])

    async def test_acls_are_still_the_local_ones(self, corpus: Path) -> None:
        """Grants come from the message's participants either way (ADR 0008)."""
        manifest, connector = await _sampled(corpus)
        external_id = manifest.message_ids[0]
        assert await connector.acls(external_id) == await LocalConnector(corpus).acls(external_id)
