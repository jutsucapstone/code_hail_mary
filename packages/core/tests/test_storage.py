"""The upload-security properties, asserted where they are decided.

Every test here is about something that has to be true before a byte is accepted, and
each one names the attack it closes. None of them needs a bucket: key construction,
filename normalisation and content sniffing are pure functions on purpose, precisely so
they can be tested without credentials and without a network.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from jutsu_core.storage import (
    MAX_UPLOAD_BYTES,
    MisconfiguredStorage,
    ObjectStore,
    SigningIdentity,
    normalise_filename,
    object_key,
    sniff_mime,
)

ORG = UUID("11111111-1111-4111-8111-111111111111")
FILE = UUID("22222222-2222-4222-8222-222222222222")


class TestTheKeyCannotBeInfluenced:
    """Path traversal and object overwrite are unrepresentable, not filtered."""

    def test_it_is_built_from_two_server_chosen_uuids(self) -> None:
        assert object_key(ORG, FILE) == f"org/{ORG}/{FILE}"

    def test_no_filename_reaches_it(self) -> None:
        # The signature takes UUIDs. There is no parameter a filename could arrive in,
        # which is the actual defence — a sanitiser can be forgotten at a new call site.
        import inspect

        parameters = list(inspect.signature(object_key).parameters)
        assert parameters == ["org_id", "file_id"]

    def test_two_files_never_collide(self) -> None:
        other = UUID("33333333-3333-4333-8333-333333333333")

        assert object_key(ORG, FILE) != object_key(ORG, other)
        assert object_key(ORG, FILE) != object_key(other, FILE)


class TestAFilenameCannotBecomeAPathOrALie:
    @pytest.mark.parametrize(
        "hostile",
        [
            "../../etc/passwd",
            "..\\..\\windows\\system32\\config\\sam",
            "/absolute/path.txt",
            "nested/dir/file.txt",
        ],
    )
    def test_separators_are_removed(self, hostile: str) -> None:
        cleaned = normalise_filename(hostile)

        assert "/" not in cleaned
        assert "\\" not in cleaned

    def test_control_characters_are_removed(self) -> None:
        # A NUL truncates a C string, and a newline forges a line in anything that logs
        # or renders the name.
        cleaned = normalise_filename("report\x00.pdf\nInjected: yes")

        assert "\x00" not in cleaned
        assert "\n" not in cleaned

    def test_a_right_to_left_override_cannot_disguise_an_extension(self) -> None:
        # The classic listing attack: U+202E makes `exe.txt` render as `txt.exe`. NFKC
        # plus the control-character strip removes the override rather than displaying it.
        cleaned = normalise_filename("invoice‮gnp.exe")

        assert "‮" not in cleaned

    def test_ligatures_normalise_so_sorting_cannot_split_one_name_in_two(self) -> None:
        assert normalise_filename("ﬁle.pdf") == normalise_filename("file.pdf")

    def test_it_is_bounded(self) -> None:
        assert len(normalise_filename("a" * 5_000)) <= 255

    def test_a_name_made_entirely_of_hostile_characters_still_yields_something(self) -> None:
        # An empty display name would render as a blank row the reader cannot act on.
        assert normalise_filename("///\x00\x01") == "untitled"
        assert normalise_filename("") == "untitled"

    def test_a_leading_dot_is_dropped(self) -> None:
        assert not normalise_filename(".hidden").startswith(".")


class TestTheBytesAreTheAuthority:
    """Content-type spoofing is caught by reading the content."""

    @pytest.mark.parametrize(
        ("head", "expected"),
        [
            (b"%PDF-1.7\n...", "application/pdf"),
            (b"\x89PNG\r\n\x1a\n", "image/png"),
            (b"\xff\xd8\xff\xe0", "image/jpeg"),
            (b"GIF89a", "image/gif"),
            (b"PK\x03\x04", "application/zip"),
            (b"ID3\x04", "audio/mpeg"),
            (b"OggS\x00", "audio/ogg"),
            (b"\x1a\x45\xdf\xa3", "video/webm"),
        ],
    )
    def test_it_reads_the_signature(self, head: bytes, expected: str) -> None:
        assert sniff_mime(head) == expected

    def test_an_executable_announced_as_an_image_is_not_an_image(self) -> None:
        # `MZ` is a Windows executable. The declared type is a hint; this is the answer.
        assert sniff_mime(b"MZ\x90\x00\x03") != "image/png"

    def test_riff_is_resolved_by_its_form_because_wav_and_avi_share_four_bytes(self) -> None:
        assert sniff_mime(b"RIFF\x24\x08\x00\x00WAVEfmt ") == "audio/wav"
        assert sniff_mime(b"RIFF\x24\x08\x00\x00AVI LIST") == "video/x-msvideo"
        # A RIFF container that is neither is unknown rather than guessed at.
        assert sniff_mime(b"RIFF\x24\x08\x00\x00XXXX") is None

    def test_mp4_is_matched_at_its_offset(self) -> None:
        assert sniff_mime(b"\x00\x00\x00\x18ftypmp42") == "video/mp4"

    def test_text_has_no_signature_and_that_is_not_a_refusal(self) -> None:
        # Plain text, Markdown and CSV identify themselves by nothing at all. Treating
        # None as "reject" would make the three most obvious formats unuploadable.
        assert sniff_mime(b"email,role\nada@example.com,member\n") is None
        assert sniff_mime(b"# A heading\n") is None

    def test_a_short_read_does_not_raise(self) -> None:
        # A zero-byte object is a real thing to encounter, and sniffing must answer
        # rather than crash the worker that called it.
        assert sniff_mime(b"") is None
        assert sniff_mime(b"%P") is None


class TestConfiguration:
    def test_an_empty_bucket_name_is_refused_at_construction(self) -> None:
        # Per request would mean discovering it from a customer's failed upload.
        with pytest.raises(MisconfiguredStorage):
            ObjectStore("")

    def test_from_env_is_none_when_no_bucket_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A deployment without storage must run normally with the basket refusing, not
        # fail at import and take the whole API down.
        monkeypatch.delenv("JUTSU_BASKET_BUCKET", raising=False)

        assert ObjectStore.from_env() is None

    def test_from_env_builds_one_when_it_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JUTSU_BASKET_BUCKET", "jutsu-basket")

        store = ObjectStore.from_env()

        assert store is not None
        assert store.bucket_name == "jutsu-basket"


class TestTheSignedUrlIsACapabilityForOneObject:
    """Asserted against a fake blob, so the shape of what gets signed is pinned."""

    def test_the_size_ceiling_and_content_type_are_signed(self) -> None:
        recorded: dict[str, object] = {}

        class FakeBlob:
            def generate_signed_url(self, **kwargs: object) -> str:
                recorded.update(kwargs)
                return "https://storage.example/signed"

        class FakeBucket:
            def blob(self, key: str) -> FakeBlob:
                recorded["key"] = key
                return FakeBlob()

        class FakeClient:
            def bucket(self, name: str) -> FakeBucket:
                return FakeBucket()

        # `signer` stated, not inherited: these assert WHAT is signed, and the
        # default resolves the ambient identity, which would make the result depend
        # on whether the machine running the suite happens to hold credentials.
        store = ObjectStore("jutsu-basket", client=FakeClient(), signer=lambda: None)
        signed = store.signed_upload(object_key(ORG, FILE), content_type="application/pdf")

        assert recorded["method"] == "PUT"
        assert recorded["version"] == "v4"
        assert recorded["content_type"] == "application/pdf"
        # The ceiling rides in a SIGNED header, so GCS refuses an oversized body before
        # storing it rather than a worker discovering it after paying for the transfer.
        headers = recorded["headers"]
        assert isinstance(headers, dict)
        assert headers["x-goog-content-length-range"] == f"0,{MAX_UPLOAD_BYTES}"
        assert signed.headers == headers
        assert recorded["key"] == f"org/{ORG}/{FILE}"

    def test_a_caller_cannot_raise_the_ceiling_past_the_global_one(self) -> None:
        recorded: dict[str, object] = {}

        class FakeBlob:
            def generate_signed_url(self, **kwargs: object) -> str:
                recorded.update(kwargs)
                return "u"

        class FakeBucket:
            def blob(self, key: str) -> FakeBlob:
                return FakeBlob()

        class FakeClient:
            def bucket(self, name: str) -> FakeBucket:
                return FakeBucket()

        ObjectStore("b", client=FakeClient(), signer=lambda: None).signed_upload(
            "k", content_type="text/plain", max_bytes=MAX_UPLOAD_BYTES * 100
        )

        headers = recorded["headers"]
        assert isinstance(headers, dict)
        assert headers["x-goog-content-length-range"] == f"0,{MAX_UPLOAD_BYTES}"

    def test_the_download_filename_is_pinned_server_side_and_cannot_be_injected(self) -> None:
        # The name comes from the database column, not the request — and a quote in it
        # must not be able to break out of the Content-Disposition header.
        recorded: dict[str, object] = {}

        class FakeBlob:
            def generate_signed_url(self, **kwargs: object) -> str:
                recorded.update(kwargs)
                return "u"

        class FakeBucket:
            def blob(self, key: str) -> FakeBlob:
                return FakeBlob()

        class FakeClient:
            def bucket(self, name: str) -> FakeBucket:
                return FakeBucket()

        ObjectStore("b", client=FakeClient(), signer=lambda: None).signed_download(
            "k", filename='evil".pdf\r\nX-Injected: yes'
        )

        disposition = recorded["response_disposition"]
        assert isinstance(disposition, str)
        assert '"' not in disposition.removeprefix('attachment; filename="').removesuffix('"')
        assert "\r" not in disposition
        assert "\n" not in disposition
        assert recorded["method"] == "GET"


class TestInvisibleCharactersInFilenames:
    """The category-based strip, and why a C0 filter is not enough.

    U+202E is category `Cf`, not `Cc`, so it survives `[\x00-\x1f\x7f]` — which is what
    the first version of `normalise_filename` used, and what the test above caught.
    """

    @pytest.mark.parametrize(
        "invisible",
        [
            "\u202e",  # RIGHT-TO-LEFT OVERRIDE — the listing disguise
            "\u202a",  # LEFT-TO-RIGHT EMBEDDING
            "\u2066",  # LEFT-TO-RIGHT ISOLATE
            "\u2069",  # POP DIRECTIONAL ISOLATE
            "\u200b",  # ZERO WIDTH SPACE
            "\u200e",  # LEFT-TO-RIGHT MARK
            "\u061c",  # ARABIC LETTER MARK
            "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM
        ],
    )
    def test_every_bidi_and_format_control_is_removed(self, invisible: str) -> None:
        cleaned = normalise_filename(f"report{invisible}.pdf")

        assert invisible not in cleaned

    def test_a_separator_hidden_behind_an_override_is_still_removed(self) -> None:
        # Invisibles are stripped BEFORE separators, so a `/` that was not adjacent to
        # one cannot survive by hiding behind it.
        cleaned = normalise_filename("a\u202e/../b.txt")

        assert "/" not in cleaned
        assert "\u202e" not in cleaned


class TestTheSnifferRecognisesWhatThePickerOffers:
    """Every type the file picker offers must sniff to the type it declares.

    A mismatch here is not a cosmetic problem: `_resolve` refuses the file, and it refuses
    it in `complete_upload` — **after** the browser has PUT the whole thing to Cloud
    Storage. Four of the twenty extensions on offer used to fail exactly that way, so a
    300 MB recording was uploaded in full and then told it was not what it claimed.
    """

    def test_a_webp_image_is_recognised(self) -> None:
        # RIFF is a container: WAV, AVI and WEBP share the first four bytes, and WEBP was
        # missing from the form table, so every .webp sniffed as None.
        assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"

    def test_a_quicktime_movie_is_not_called_an_mp4(self) -> None:
        # `.mov` declares video/quicktime; a flat "video/mp4" for every ftyp box
        # disagreed with it.
        assert sniff_mime(b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00") == "video/quicktime"

    def test_m4a_audio_is_audio_rather_than_video(self) -> None:
        assert sniff_mime(b"\x00\x00\x00\x20ftypM4A \x00\x00\x02\x00") == "audio/mp4"

    def test_a_plain_mp4_still_sniffs_as_video(self) -> None:
        assert sniff_mime(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00") == "video/mp4"

    def test_an_unknown_ftyp_brand_falls_back_to_mp4(self) -> None:
        # The previous behaviour for every brand, kept for the ones not named.
        assert sniff_mime(b"\x00\x00\x00\x18ftypzzzz\x00\x00\x00\x00") == "video/mp4"

    def test_an_mp3_without_an_id3_tag_is_still_an_mp3(self) -> None:
        """An ID3 tag is optional; a bare MPEG frame is the actual signature.

        Without this a tagless `.mp3` sniffed as None and was refused for disagreeing with
        a claim it never contradicted.
        """
        assert sniff_mime(b"\xff\xfb\x90\x00" + b"\x00" * 20) == "audio/mpeg"

    def test_an_id3_tagged_mp3_is_unaffected(self) -> None:
        assert sniff_mime(b"ID3\x04\x00\x00\x00\x00\x00\x00") == "audio/mpeg"

    def test_a_wav_and_an_avi_still_resolve(self) -> None:
        assert sniff_mime(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "audio/wav"
        assert sniff_mime(b"RIFF\x00\x00\x00\x00AVI LIST") == "video/x-msvideo"

    def test_an_executable_is_still_refused(self) -> None:
        """The whole point of sniffing. Widening the table must not widen this.

        `MZ` is a DOS/PE header and matches nothing, so `_resolve` sees `None` against a
        non-text claim and refuses — which is the `.exe` announced as `image/png`.
        """
        assert sniff_mime(b"MZ\x90\x00\x03" + b"\x00" * 64) is None

    def test_a_short_read_never_raises(self) -> None:
        # `read_head` returns whatever the object had; an empty or one-byte object must
        # produce "unknown", not an IndexError inside a request.
        for head in (b"", b"\xff", b"R", b"\x00\x00\x00"):
            assert sniff_mime(head) is None or isinstance(sniff_mime(head), str)


class TestSigningWithoutAPrivateKey:
    """How a URL gets signed when the process holds no key.

    This is the defect that made the Knowledge Basket unusable in production, and it was
    invisible here: Cloud Run's ambient identity is a bearer token with nothing to sign
    with, so `generate_signed_url` raised `AttributeError: you need a private key to sign
    credentials` on every upload — while these tests passed, because a fake blob signs
    nothing. The signer is injected so that what the deployment can do is a fact the test
    states rather than a property of the machine running it.
    """

    @staticmethod
    def _recorder() -> tuple[dict[str, object], object]:
        recorded: dict[str, object] = {}

        class FakeBlob:
            def generate_signed_url(self, **kwargs: object) -> str:
                recorded.update(kwargs)
                return "https://storage.example/signed"

        class FakeBucket:
            def blob(self, key: str) -> FakeBlob:
                return FakeBlob()

        class FakeClient:
            def bucket(self, name: str) -> FakeBucket:
                return FakeBucket()

        return recorded, FakeClient()

    def test_an_upload_signs_through_iam_when_there_is_no_key(self) -> None:
        recorded, client = self._recorder()
        identity = SigningIdentity(
            service_account_email="jutsu-runtime@example.iam.gserviceaccount.com",
            access_token="fake-access-token",
        )

        ObjectStore("b", client=client, signer=lambda: identity).signed_upload(
            object_key(ORG, FILE), content_type="application/pdf"
        )

        # Both, or neither: the storage library falls back to a local key unless it is
        # given an account AND a token to call signBlob with.
        assert recorded["service_account_email"] == identity.service_account_email
        assert recorded["access_token"] == "fake-access-token"

    def test_a_download_signs_through_iam_too(self) -> None:
        """`signed_download` had the identical defect, so downloads were broken by the
        same cause and would have stayed broken had only the upload been fixed."""
        recorded, client = self._recorder()
        identity = SigningIdentity(
            service_account_email="jutsu-runtime@example.iam.gserviceaccount.com",
            access_token="fake-access-token",
        )

        ObjectStore("b", client=client, signer=lambda: identity).signed_download(
            "k", filename="notes.pdf"
        )

        assert recorded["service_account_email"] == identity.service_account_email
        assert recorded["access_token"] == "fake-access-token"

    def test_a_process_holding_a_key_signs_in_process(self) -> None:
        """A service-account JSON key signs locally, needs no API call, and must not be
        handed an access token — passing one would route a working local signature
        through a network round trip it does not need."""
        recorded, client = self._recorder()

        ObjectStore("b", client=client, signer=lambda: None).signed_upload(
            "k", content_type="text/plain"
        )

        assert "service_account_email" not in recorded
        assert "access_token" not in recorded


class TestTheSigningTokenIsNotCachedPastItsLife:
    """An access token is good for about an hour.

    Caching the identity built from one is the worst shape a bug can have: every check
    made just after a deploy passes, and uploads start failing an hour later for as long
    as the revision lives. So the credentials are cached and the identity is not.
    """

    @staticmethod
    def _fake_credentials() -> object:
        class FakeCredentials:
            def __init__(self) -> None:
                self.service_account_email = "jutsu-runtime@example.iam.gserviceaccount.com"
                self.token = "first-token"
                self.valid = False
                self.refreshes = 0

            def refresh(self, request: object) -> None:
                self.refreshes += 1
                self.token = f"token-{self.refreshes}"
                self.valid = True

        return FakeCredentials()

    def test_a_lapsed_token_is_refreshed_rather_than_reused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jutsu_core import storage

        credentials = self._fake_credentials()
        monkeypatch.setattr(storage, "_ambient_credentials", credentials)

        first = storage.ambient_signing_identity()
        assert first is not None
        assert first.access_token == "token-1"

        # Still valid: no second round trip, same token.
        second = storage.ambient_signing_identity()
        assert second is not None
        assert second.access_token == "token-1"
        assert credentials.refreshes == 1  # type: ignore[attr-defined]

        # An hour later.
        credentials.valid = False  # type: ignore[attr-defined]
        third = storage.ambient_signing_identity()
        assert third is not None
        assert third.access_token == "token-2", "a lapsed token must not be handed out again"

    def test_credentials_that_name_no_account_are_refused_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Half an identity fails deep inside the storage library with a message about
        private keys. Refusing here says what is actually wrong."""
        from jutsu_core import storage

        class Nameless:
            service_account_email = ""
            token = "t"
            valid = True

            def refresh(self, request: object) -> None:
                return None

        monkeypatch.setattr(storage, "_ambient_credentials", Nameless())

        with pytest.raises(MisconfiguredStorage):
            storage.ambient_signing_identity()
