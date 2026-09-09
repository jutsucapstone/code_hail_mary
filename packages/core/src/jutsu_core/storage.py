"""Object storage for the Knowledge Basket: keys the server owns, URLs that expire.

Bytes live in one Google Cloud Storage bucket; `basket_files` decides who may reach them
(ADR 0020). Nothing here makes an authorization decision — a signed URL is minted only
after a row-level-security-scoped query has already succeeded, and this module's job is
to make the *mechanics* of that unable to betray the decision.

**The object key contains no user input.** `org/{org_id}/{file_id}`, both UUIDs the server
chose. Path traversal, object overwrite and key collision are not filtered here; they are
unrepresentable, because there is nothing to filter. The filename the person uploaded is a
database column and never touches a path.

**No service-account key exists.** V4 signing goes through the IAM Credentials API
(`signBlob`) using the runtime service account's own identity, so no private key is
generated, stored in Secret Manager, or shipped in an image. That is the difference
between "we use signed URLs" and "we have a private key in production".

`google-cloud-storage` is imported LAZILY, matching `jutsu_core.doorbell`'s contract with
`google-cloud-tasks`: `import jutsu_core` must stay cheap and side-effect free, and the
API process that never serves a basket route should not pay for the client at startup.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

__all__ = [
    "ENV_BUCKET",
    "MAX_UPLOAD_BYTES",
    "MisconfiguredStorage",
    "ObjectStore",
    "SignedUpload",
    "SigningIdentity",
    "ambient_signing_identity",
    "normalise_filename",
    "object_key",
    "sanitise_original",
    "sniff_mime",
]

ENV_BUCKET: Final = "JUTSU_BASKET_BUCKET"

#: The largest object the signed URL will admit, and the same ceiling the database's
#: `ck_basket_files_size` enforces. 512 MiB is past any realistic recording and far short
#: of a size that would make one row a problem.
MAX_UPLOAD_BYTES: Final = 512 * 1024 * 1024

#: How long an upload URL is good for. Long enough for a large file on a poor connection,
#: short enough that a leaked URL is not a standing grant.
_UPLOAD_TTL: Final = timedelta(minutes=30)

#: How long a download URL is good for. Minted per request after an authorization check,
#: so this only has to outlive the click that follows.
_DOWNLOAD_TTL: Final = timedelta(minutes=5)


class MisconfiguredStorage(RuntimeError):
    """The bucket is half-configured.

    Raised at construction rather than per request, for the reason
    `MisconfiguredDoorbell` exists: a storage layer that fails on the hundredth upload
    because an environment variable was never set is a deployment problem discovered by a
    customer.
    """


@dataclass(frozen=True, slots=True)
class SignedUpload:
    """Everything the browser needs, and nothing it could misuse."""

    url: str
    #: Headers the browser MUST send. They are part of what was signed, so a client that
    #: changes the content type or exceeds the size is refused by GCS, not by us.
    headers: dict[str, str]
    expires_in_seconds: int


def object_key(org_id: UUID, file_id: UUID) -> str:
    """The only place an object key is constructed.

    `org_id` leads so a misconfiguration is auditable by prefix — it is not what enforces
    isolation, and reading it as a security boundary is the mistake this docstring exists
    to prevent. The row in `basket_files` under FORCE row-level security is the boundary.
    """
    return f"org/{org_id}/{file_id}"


#: Directory separators. Removed rather than escaped — there is no context here in which
#: either could be legitimate, because the server chose the key this file lives under.
_SEPARATORS: Final = re.compile(r"[/\\]+")
_COLLAPSE: Final = re.compile(r"\s+")


def _strip_invisibles(value: str) -> str:
    """Drop every control and format character, by Unicode category.

    **A character class of `\\x00-\\x1f\\x7f` is not enough, and the gap is the interesting
    one.** U+202E RIGHT-TO-LEFT OVERRIDE is category `Cf`, not `Cc`, so it survives a C0
    filter — and it is the classic file-listing disguise: `invoice\\u202egnp.exe` renders
    as `invoiceexe.png`, so the reader sees an image and opens an executable. The same
    applies to the bidi isolates (U+2066 to U+2069), U+200B ZERO WIDTH SPACE and U+FEFF.

    Categories rather than a list, because the list is what gets out of date: `Cc` is
    every control, `Cf` is every format character, and neither has any business in a name
    a person is meant to read.
    """
    return "".join(ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf"))


#: Long enough for any real filename, short enough to render in a table.
_MAX_FILENAME: Final = 255


def sanitise_original(raw: str) -> str:
    """The person's own filename, kept as they typed it — minus what cannot be stored.

    This is the DISPLAY and DOWNLOAD name, so case, spaces and punctuation all survive;
    only the characters that are not text survive nothing. That is not tidiness:

      * **Postgres refuses `\\x00` in a text column outright**, so a filename containing
        one is a failed INSERT and a 500 rather than a stored file. Found by a test that
        uploaded `with\\x00nul.txt`.
      * A bidi override in a name the interface renders is the listing disguise
        `normalise_filename` already removes; leaving it in the original would put it
        back on screen.

    Separators are deliberately KEPT here. They cannot reach an object key — the server
    chooses that — and `signed_download` strips what would break a Content-Disposition
    header, so a name like `q3/q4 notes.txt` displays as the person wrote it.
    """
    cleaned = _strip_invisibles(unicodedata.normalize("NFC", raw or "")).strip()
    return cleaned[:_MAX_FILENAME] or "untitled"


def normalise_filename(raw: str) -> str:
    """A display and sort key that cannot be a path or a lie.

    The original is kept verbatim in its own column for download; this is what the
    interface lists, sorts and searches. Directory separators and control characters are
    removed rather than escaped, because there is no context here in which either could
    be legitimate — the key this file lives under was chosen by the server.

    NFKC first: `ﬁle.pdf` with a ligature and `file.pdf` must not sort as two different
    things, and a right-to-left override in a filename must not be able to make `.exe`
    read as `.txt` in a listing.
    """
    cleaned = unicodedata.normalize("NFKC", raw or "")
    # Invisibles first: a separator hidden behind a bidi override must be removed as a
    # separator, not left because it was not adjacent to one.
    cleaned = _strip_invisibles(cleaned)
    cleaned = _SEPARATORS.sub(" ", cleaned)
    cleaned = _COLLAPSE.sub(" ", cleaned).strip()
    # Leading dots make a file invisible on POSIX and confuse extension parsing.
    cleaned = cleaned.lstrip(".").strip()
    return (cleaned[:_MAX_FILENAME] or "untitled").lower()


#: Magic bytes, checked against what the client DECLARED.
#:
#: A declared content type is a hint used to pin the signed URL and nothing more. The
#: bytes are the authority, and disagreement is evidence rather than a formatting detail:
#: an `.exe` announced as `image/png` is the oldest upload attack there is.
#:
#: `(offset, signature, mime)`. Container formats that share a signature — every OOXML
#: document and every ZIP is `PK\x03\x04` — resolve to the archive type here and are
#: separated by the caller, which knows the declared type and the extension.
_SIGNATURES: Final[tuple[tuple[int, bytes, str], ...]] = (
    (0, b"%PDF-", "application/pdf"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"PK\x03\x04", "application/zip"),
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
    (0, b"ID3", "audio/mpeg"),
    (0, b"OggS", "audio/ogg"),
    (0, b"\x1a\x45\xdf\xa3", "video/webm"),
    (0, b"RIFF", "riff"),
    # Resolved by brand below — a flat "video/mp4" refused every .mov and .m4a.
    (4, b"ftyp", "ftyp"),
)


#: ISO base media file format brands, read from bytes 8-12 immediately after `ftyp`.
#:
#: Returning a flat `video/mp4` for every `ftyp` file was wrong in a way that cost a whole
#: upload: `.mov` declares `video/quicktime` and `.m4a` declares `audio/mp4`, both
#: disagreed with the flat answer, and `_resolve` refused them — **after** the browser had
#: PUT the entire file to Cloud Storage. A 300 MB recording was uploaded in full and then
#: told it was not what it claimed.
#:
#: Unknown brands fall back to `video/mp4`, which is what the vast majority of `ftyp`
#: files are and what the previous behaviour assumed for all of them.
_FTYP_BRANDS: Final[dict[bytes, str]] = {
    b"qt  ": "video/quicktime",
    b"M4A ": "audio/mp4",
    b"M4B ": "audio/mp4",
    b"M4P ": "audio/mp4",
    b"M4V ": "video/x-m4v",
    b"3gp4": "video/3gpp",
    b"3gp5": "video/3gpp",
    b"avif": "image/avif",
    b"heic": "image/heic",
    b"heix": "image/heic",
    b"mif1": "image/heif",
}


def sniff_mime(head: bytes) -> str | None:
    """What the bytes actually are, or None when nothing matched.

    None is not a refusal — plain text, Markdown and CSV have no magic number and are
    perfectly legitimate. It means "the content does not identify itself", and the caller
    decides whether the declared type is one where that is expected.
    """
    for offset, signature, mime in _SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            if mime == "riff":
                # RIFF is a container and the four bytes at 8 are the form. WEBP was
                # missing here, so every `.webp` sniffed as None, disagreed with its
                # declared `image/webp`, and was rejected after a completed upload.
                form = head[8:12]
                if form == b"WAVE":
                    return "audio/wav"
                if form == b"AVI ":
                    return "video/x-msvideo"
                if form == b"WEBP":
                    return "image/webp"
                return None
            if mime == "ftyp":
                return _FTYP_BRANDS.get(head[8:12], "video/mp4")
            return mime
    # An MP3 need not carry an ID3 tag; a bare MPEG audio frame begins with eleven set
    # bits. Without this a tagless `.mp3` sniffed as None and was refused for disagreeing
    # with a claim it never contradicted. The second byte's top three bits are the sync
    # remainder, and 0xE0 is the mask that reads them.
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    return None


@dataclass(frozen=True)
class SigningIdentity:
    """The account a V4 signature is made as, and a token authorising the signing call.

    Present only when the process holds no private key. `generate_signed_url` given both
    of these signs through the IAM Credentials API (`signBlob`) instead of locally.
    """

    service_account_email: str
    access_token: str


#: The CREDENTIALS are cached, never the identity built from them: an access token is
#: good for about an hour, and a process that cached the token would sign correctly until
#: it lapsed and then fail for as long as the revision lived. Resolving the credentials is
#: the expensive part (a metadata-server round trip); refreshing them is the cheap part
#: google-auth already skips when they are still valid.
_ambient_credentials: Any = None
_ambient_lock = threading.Lock()


def ambient_signing_identity() -> SigningIdentity | None:
    """How this process signs, or `None` when it can sign in-process.

    **This is the difference between the Knowledge Basket working and not.** Cloud Run's
    ambient identity is `compute_engine.Credentials` — a bearer token and nothing to sign
    with — and `generate_signed_url` answers that with `AttributeError: you need a private
    key to sign credentials` rather than degrading. Every upload therefore 500'd in
    production while every test passed, because a test injects a fake blob that signs
    nothing. Returning the account's email and a live token moves the signature to
    `signBlob`, which is what this module's docstring has always described and what the
    runtime account was already granted (`roles/iam.serviceAccountTokenCreator`, on
    itself).

    `None` means the credentials implement `Signing` — a service-account JSON key, which
    is how a developer runs this locally. Signing in-process is free and needs no API, so
    the local path is left exactly as it was.

    Re-read on every call rather than cached: the token is good for about an hour, and a
    cached one would sign correctly until it lapsed and then fail for the life of the
    revision — the worst shape a bug can have, because it passes every check made just
    after a deploy.
    """
    global _ambient_credentials

    from google.auth import credentials as ga_credentials
    from google.auth.transport.requests import Request

    with _ambient_lock:
        if _ambient_credentials is None:
            from google.auth import default as ambient_credentials

            # Signing through IAM is a cloud-platform call; the metadata server ignores
            # the request and returns the instance's own scopes, which already include
            # it. `Any` for the same reason `_bucket` uses it: google-auth ships no
            # annotations, and mypy's strictness would otherwise spread
            # `no-untyped-call` through every caller.
            resolved, _ = ambient_credentials(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            _ambient_credentials = resolved

        credentials: Any = _ambient_credentials
        # The library's own test, so this cannot drift from what it will accept:
        # `ensure_signed_credentials` raises for anything that is not `Signing`.
        if isinstance(credentials, ga_credentials.Signing):
            return None

        # Every call, because the token behind it expires. `valid` is false when it has
        # lapsed or was never fetched, so this is one metadata round trip an hour rather
        # than one per signature.
        if not credentials.valid:
            credentials.refresh(Request())

        email = getattr(credentials, "service_account_email", "")
        if not email or not credentials.token:
            # Nothing usable to sign with. Say so where it can be read, rather than
            # returning half an identity that fails inside the storage library.
            raise MisconfiguredStorage(
                "the runtime credentials can neither sign locally nor name an account "
                "for the IAM signBlob API"
            )
        return SigningIdentity(service_account_email=email, access_token=credentials.token)


class ObjectStore:
    """The bucket, and the four things anything is allowed to do with it.

    Constructed once per process. `from_env` returns None when no bucket is configured,
    so a deployment without storage runs normally with the basket routes refusing — rather
    than failing at import and taking the whole API down.
    """

    def __init__(
        self,
        bucket: str,
        *,
        client: Any | None = None,
        signer: Callable[[], SigningIdentity | None] = ambient_signing_identity,
    ) -> None:
        if not bucket:
            raise MisconfiguredStorage(f"{ENV_BUCKET} is empty")
        self._bucket_name = bucket
        self._client = client
        # Injected for the same reason `client` is: how this process signs is a property
        # of the deployment, and a test that resolved it from the ambient environment
        # would pass or fail on whether the machine running it happened to hold
        # credentials. The default is the real one.
        self._signer = signer

    @classmethod
    def from_env(cls) -> ObjectStore | None:
        bucket = os.environ.get(ENV_BUCKET, "").strip()
        return cls(bucket) if bucket else None

    @property
    def bucket_name(self) -> str:
        return self._bucket_name

    def _bucket(self) -> Any:
        if self._client is None:
            # Lazy, per the module docstring: `import jutsu_core` stays cheap.
            # `google.cloud` is a namespace package, so mypy cannot resolve the `storage`
            # submodule through it without vendored stubs.
            from google.cloud import storage  # type: ignore[attr-defined]

            self._client = storage.Client()
        return self._client.bucket(self._bucket_name)

    def _signing(self) -> dict[str, str]:
        """The two arguments that make V4 signing work without a private key.

        Empty when the process can sign in-process, which keeps the local and test paths
        byte-for-byte what they were.
        """
        identity = self._signer()
        if identity is None:
            return {}
        return {
            "service_account_email": identity.service_account_email,
            "access_token": identity.access_token,
        }

    def signed_upload(
        self, key: str, *, content_type: str, max_bytes: int = MAX_UPLOAD_BYTES
    ) -> SignedUpload:
        """A capability for ONE object, of one type, up to one size, for thirty minutes.

        `x-goog-content-length-range` is signed, so the ceiling is enforced by GCS before
        a byte is stored — not discovered afterwards by a worker that has already paid for
        the transfer. `content_type` is signed for the same reason: the browser cannot
        change it after the fact, which is what makes the declared type worth recording as
        evidence when the sniffed type disagrees.
        """
        bounded = max(1, min(max_bytes, MAX_UPLOAD_BYTES))
        headers = {
            "Content-Type": content_type,
            "x-goog-content-length-range": f"0,{bounded}",
        }
        url = (
            self._bucket()
            .blob(key)
            .generate_signed_url(
                version="v4",
                expiration=_UPLOAD_TTL,
                method="PUT",
                content_type=content_type,
                headers=headers,
                **self._signing(),
            )
        )
        return SignedUpload(
            url=url, headers=headers, expires_in_seconds=int(_UPLOAD_TTL.total_seconds())
        )

    def signed_download(self, key: str, *, filename: str) -> str:
        """A short-lived GET, with the download filename pinned server-side.

        `response_disposition` carries the original name so the person gets the file they
        uploaded rather than a UUID — and it is set HERE, from the database column, so a
        client cannot choose what a downloaded file is called.
        """
        safe = filename.replace('"', "").replace("\\", "").replace("\r", "").replace("\n", "")
        # Annotated on the way out: `generate_signed_url` is untyped, and mypy's
        # `warn_return_any` is what stops that `Any` spreading into every caller.
        url: str = (
            self._bucket()
            .blob(key)
            .generate_signed_url(
                version="v4",
                expiration=_DOWNLOAD_TTL,
                method="GET",
                response_disposition=f'attachment; filename="{safe}"',
                **self._signing(),
            )
        )
        return url

    def stat(self, key: str) -> tuple[int, str] | None:
        """`(size, crc32c)` as GCS computed them, or None if the object is not there.

        Read back rather than trusted: the client declares a size when it asks for the
        URL, and the only number worth writing to the database is the one the store
        measured.
        """
        blob = self._bucket().get_blob(key)
        if blob is None:
            return None
        return int(blob.size or 0), str(blob.crc32c or "")

    def read_head(self, key: str, *, count: int = 4096) -> bytes:
        """The first bytes, for sniffing. Never the whole object."""
        blob = self._bucket().blob(key)
        return bytes(blob.download_as_bytes(start=0, end=max(0, count - 1)))

    def download(self, key: str, *, max_bytes: int) -> bytes:
        """The whole object, bounded.

        The bound is the caller's, not the file's: an extractor that can only handle ten
        megabytes must not be handed five hundred because the upload ceiling allowed it.
        """
        blob = self._bucket().blob(key)
        return bytes(blob.download_as_bytes(start=0, end=max(0, max_bytes - 1)))

    def delete(self, key: str) -> None:
        """Remove the object. Missing is success — deletion is idempotent by design."""
        from google.api_core import exceptions

        try:
            self._bucket().blob(key).delete()
        except exceptions.NotFound:
            return
